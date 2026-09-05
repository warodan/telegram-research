"""tgparse.py -- turning the three raw surfaces into records.

Every assertion below was run against the current parser and the cited probe
before being written down. Where a value was not verified that way, this
file states how it was obtained instead of asserting it on faith.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import os
import re
from pathlib import Path

import pytest

import tgdom
import tgparse
import tgweb

# One live page, saved on 2026-08-24 to settle what a zero-hit `?q=` search
# actually looks like. It could not go in the probes directory (frozen), so
# it lives with the repair it proves.
#
# `TELEGRAM_RESEARCH_PAGES` matches the `TELEGRAM_RESEARCH_PROBES` override in
# conftest.py, whose docstring promises the suite can run against a relocated
# copy of the skill. Without an override here that promise was false.
PAGES = Path(os.environ.get("TELEGRAM_RESEARCH_PAGES")
                or (Path(__file__).resolve().parents[0] / "fixtures" / "pages"))
NO_HITS_PAGE = PAGES / "live-2026-08-24-s-durov-q-nohits.html"

_CORPUS_CACHE: list | None = None


def corpus_messages(fixtures: Path) -> list:
    """`(probe name, Message)` for every message either parser finds anywhere.

    Both routes over every probe file, which is how the 122 were counted:
    107 from the `/s/` pages plus 15 single messages. Parsed
    once and cached -- three tests assert different properties of the same set.
    """
    global _CORPUS_CACHE
    if _CORPUS_CACHE is None:
        out = []
        for path in sorted(fixtures.iterdir()):
            body = path.read_text(encoding="utf-8", errors="replace")
            found = list(tgparse.parse_preview(body, "x").messages)
            # `embed_unreadable` is asked first only to keep the sweep quiet:
            # most probes are not embed pages at all, and parse_embed now says
            # so on stderr rather than returning None as if the id were empty.
            single = None if tgweb.embed_unreadable(body) else tgparse.parse_embed(body, "x", 1)
            if single is not None:
                found.append(single)
            out += [(path.name, msg) for msg in found]
        _CORPUS_CACHE = out
    return _CORPUS_CACHE


# --------------------------------------------------------------------------
# A01 -- the one full real channel page, 20 messages with a gap at 530
# --------------------------------------------------------------------------
def test_a01_message_count_and_id_range(probe):
    page = tgparse.parse_preview(probe("A01-s-durov.html"), "durov")
    ids = sorted(m.id for m in page.messages)
    assert len(ids) == 20
    assert ids[0] == 523
    assert ids[-1] == 543
    assert 530 not in ids  # the gap: 523..543 is 21 numbers, 20 messages


def test_a01_cursors_and_chat_id(probe):
    page = tgparse.parse_preview(probe("A01-s-durov.html"), "durov")
    assert page.before == 523
    assert page.after is None
    assert page.chat_id == -1006503122
    # the page-level chat_id is backfilled onto every message that lacks one
    assert all(m.chat_id == -1006503122 for m in page.messages)


def test_a01_first_message_fields(probe):
    page = tgparse.parse_preview(probe("A01-s-durov.html"), "durov")
    m0 = min(page.messages, key=lambda m: m.id)
    assert m0.date == "2026-06-09T19:33:51+00:00"
    assert m0.views_raw == "12.5M"
    assert m0.views == 12_500_000
    # The video plays in Telegram and the widget says this browser cannot render
    # it. Both are true and both are recorded; neither makes it a service
    # message (frozen contract, item 11).
    assert m0.media == ["video", "unsupported:video"]
    # THE one-line assertion that would have caught the shipped is_service bug
    # on the day the parser was written, and that no test in the suite made.
    assert m0.is_service is False
    assert m0.text.startswith("⌚️ A fully native Telegram app for Apple Watch")
    assert m0.reactions == {
        "custom:5465587407350942612": "55.2K",
        "custom:5265077361648368841": "18.6K",
        "custom:5399847211989246390": "13.4K",
        "custom:5936157098181135162": "825",
        "custom:5373223594484587136": "220",
    }
    # Every reaction key is a custom-emoji id: the channel page renders reaction
    # emoji as <tg-emoji emoji-id="..."></tg-emoji> with no character at all.
    assert all(key.startswith("custom:") for key in m0.reactions)


# --------------------------------------------------------------------------
# The search= vs q= finding: search= is silently ignored server-side
# --------------------------------------------------------------------------
def test_search_param_is_ignored_but_q_param_is_not(probe):
    a01_ids = sorted(m.id for m in tgparse.parse_preview(probe("A01-s-durov.html"), "durov").messages)
    c04_ids = sorted(m.id for m in tgparse.parse_preview(probe("C04-s-durov-search.html"), "durov").messages)
    c03_ids = sorted(m.id for m in tgparse.parse_preview(probe("C03-s-durov-q.html"), "durov", found_by="telegram").messages)
    # ?search=telegram (C04) comes back identical to the unfiltered base page:
    # the parameter was never read server-side.
    assert c04_ids == a01_ids
    # ?q=telegram (C03) is a real filter and returns a different id set.
    assert c03_ids != a01_ids


def test_c15_rare_query_exact_ids(probe):
    page = tgparse.parse_preview(probe("C15-s-durov-q-rare.html"), "durov", found_by="bitcoin")
    assert sorted(m.id for m in page.messages) == [62, 67, 77, 116, 215, 232, 440]


# --------------------------------------------------------------------------
# C10 -- group embed with a reply block
# --------------------------------------------------------------------------
def test_c10_reply_embed(probe):
    m = tgparse.parse_embed(probe("C10-embed-tdlibchat-10000.html"), "tdlibchat", 10000)
    assert m is not None
    assert m.author_name == "Author One"
    assert m.author_username == "redacted_user_01"
    assert m.reply_to_author == "\U0001f4bb Author Two"
    assert m.reply_to_id == 9999
    assert m.text.startswith("If you set permissions to default group permissions")


def test_c10_reply_block_never_leaks_into_message_text(probe):
    # tgparse.py's own comment: the body text and the quoted reply text share a
    # class and are told apart only by the js- twin; reading the wrong one
    # silently replaces the post with the post it answered. Measured exactly
    # on this fixture.
    m = tgparse.parse_embed(probe("C10-embed-tdlibchat-10000.html"), "tdlibchat", 10000)
    assert m.reply_to_text == "Default group permissionss"
    assert m.reply_to_text not in m.text
    assert m.text != m.reply_to_text


# --------------------------------------------------------------------------
# C07 -- the group service message
# --------------------------------------------------------------------------
def test_c07_service_message_basic_fields(probe):
    m = tgparse.parse_embed(probe("C07-embed-tdlibchat-1.html"), "tdlibchat", 1)
    assert m is not None
    assert m.id == 1
    assert m.username == "tdlibchat"
    # The widget shows this block as "Service message" (message_media_not_supported_label)
    # and carries no js-message_text node, so there is nothing to extract as text.
    assert m.text == ""


def test_c07_is_recognised_as_a_service_message(probe):
    m = tgparse.parse_embed(probe("C07-embed-tdlibchat-1.html"), "tdlibchat", 1)
    assert m.is_service is True
    # Paired deliberately with an ordinary message from the SAME surface. On its
    # own the assertion above is satisfied by a flag that is unconditionally
    # true, which is exactly what shipped: every one of the 122 messages in the
    # probe corpus was flagged and this test stayed green.
    ordinary = tgparse.parse_embed(probe("C10-embed-tdlibchat-10000.html"), "tdlibchat", 10000)
    assert ordinary.is_service is False


def test_durov_video_post_is_not_a_service_message(probe):
    # C05 carries a message_media_not_supported_wrap too -- nested in the video
    # player, where it means "your browser cannot play this". That is a fact
    # about the browser, not a service event.
    m = tgparse.parse_embed(probe("C05-embed-durov-523.html"), "durov", 523)
    assert m.is_service is False
    assert "unsupported:video" in m.media


def test_service_messages_across_the_whole_probe_corpus(fixtures):
    """Exactly 4 of the 122 messages in the 32 saved probes, and which 4.

    The corpus is every message either parser produces from every probe file,
    counted the way the 122 were counted: 107 from the `/s/` pages
    plus 15 single messages. The shipped selector -- `text_not_supported_wrap`,
    a styling class carried by every ordinary message div -- scored 122 of 122
    here while the whole suite stayed green.
    """
    corpus = corpus_messages(fixtures)
    service = [(name, f"{m.username}/{m.id}") for name, m in corpus if m.is_service]

    assert len(corpus) == 122, f"the probe corpus changed shape: {len(corpus)} messages"
    assert service == [
        ("A09-s-Astana_motoriders.html", "Astana_motoriders/97"),      # pinned event
        ("C07-embed-tdlibchat-1.html", "tdlibchat/1"),
        ("C11-embed-tdlibchat-100000.html", "tdlibchat/100000"),
        ("C16-embed-birding-1.html", "birding_chats/1"),
    ]


def test_a09_pinned_event_is_the_only_service_message_on_its_page(probe):
    page = tgparse.parse_preview(probe("A09-s-Astana_motoriders.html"), "Astana_motoriders")
    assert len(page.messages) == 20
    assert [m.id for m in page.messages if m.is_service] == [97]


def test_the_generic_footer_is_not_a_service_marker(probe):
    # "Please open Telegram to view this post" sits under media_not_supported_cont
    # on every post of every page: 66 of them in the corpus, none a service event.
    body = probe("A01-s-durov.html")
    assert "media_not_supported_cont" in body
    page = tgparse.parse_preview(body, "durov")
    assert [m.id for m in page.messages if m.is_service] == []


def test_media_never_carries_a_browser_notice_as_a_type(fixtures):
    # A caption-less video used to come back as
    # media=['unsupported:this media is not supported in your browser', 'video'].
    allowed = {"photo", "video", "document", "voice", "sticker", "poll",
               "location", "unsupported:video"}
    for name, msg in corpus_messages(fixtures):
        assert set(msg.media) <= allowed, (name, msg.id, msg.media)


def test_a09_unplayable_video_is_media_not_a_service_message(probe):
    page = tgparse.parse_preview(probe("A09-s-Astana_motoriders.html"), "Astana_motoriders")
    m101 = next(m for m in page.messages if m.id == 101)
    assert m101.is_service is False
    assert m101.media == ["video", "unsupported:video"]


# --------------------------------------------------------------------------
# media_urls -- the sender's avatar is not the message's media
# --------------------------------------------------------------------------
def test_text_only_posts_carry_no_media_urls(probe):
    # Measured on A09: 97, 103 and 106 each came back carrying the channel
    # avatar as their media, and 95 the avatar plus a link-preview thumbnail.
    page = tgparse.parse_preview(probe("A09-s-Astana_motoriders.html"), "Astana_motoriders")
    by_id = {m.id: m for m in page.messages}
    for mid in (95, 97, 103, 106):
        assert by_id[mid].media == [], mid
        assert by_id[mid].media_urls == [], (mid, by_id[mid].media_urls)


def test_an_animated_profile_photo_is_not_the_posts_media(probe):
    # C16/1000 is one line of text. The .mp4 that used to land in media_urls is
    # the SENDER's animated profile photo, inside tgme_widget_message_user_photo.
    body = probe("C16-embed-birding-1000.html")
    m = tgparse.parse_embed(body, "birding_chats", 1000)
    assert m.text == "The hides with no doors, is that the ones?"
    assert m.media == []
    assert m.media_urls == []

    # The sender is a private person, so the fixture ships a placeholder data:
    # URI in place of their real profile video -- and the collector only ever
    # looks at telesco.pe, which would let the assertions above pass even with
    # the exclusion deleted. Put a CDN URL back into the same node: the avatar
    # is skipped for its class, not for its address.
    live = re.sub(r'src="data:video/mp4[^"]*"',
                  'src="https://cdn4.telesco.pe/file/animated-avatar.mp4"', body)
    assert "cdn4.telesco.pe/file/animated-avatar.mp4" in live
    m = tgparse.parse_embed(live, "birding_chats", 1000)
    assert m.media == []
    assert m.media_urls == []


def test_a_real_media_post_keeps_its_urls(probe):
    page = tgparse.parse_preview(probe("A01-s-durov.html"), "durov")
    m0 = min(page.messages, key=lambda m: m.id)
    assert m0.media_urls, "the video post must still expose its CDN files"
    assert all("telesco.pe" in url for url in m0.media_urls)


def test_no_message_anywhere_claims_media_urls_without_media(fixtures):
    for name, msg in corpus_messages(fixtures):
        real = [k for k in msg.media if not k.startswith("unsupported:")]
        assert not (msg.media_urls and not real), (name, msg.id, msg.media_urls)


# --------------------------------------------------------------------------
# cursors -- rel="prev", never rel="canonical"
# --------------------------------------------------------------------------
def test_terminal_search_page_does_not_cite_its_own_url_as_the_cursor(probe):
    # C15 is the last page of durov?q=bitcoin: 7 posts, ids 62..440, and no
    # <link rel="prev">. The first before= href in the body is rel="canonical",
    # before=441 -- the page already in hand, which refetches to itself.
    body = probe("C15-s-durov-q-rare.html")
    assert 'rel="prev"' not in body
    assert 'href="/s/durov?q=bitcoin&before=441"' in body
    page = tgparse.parse_preview(body, "durov", found_by="bitcoin")
    assert page.before != 441


def test_a_terminal_page_publishes_no_cursor_at_all(probe):
    """C15 is the last page of its search and must say so ON that page.

    The min-id fallback used to fire here too, so `page.before` was 62 -- and
    `page.before is None` was therefore unreachable while a page had messages.
    `read.walk_channel` / `search_channel` could never take their "the surface
    published no further cursor" branch on the page that said so; they took it
    one request later, on the empty page that follows. Every walk in the skill
    spent one request past the true end of the history, and on a channel whose
    oldest page carries no id <= 1 the walk ended on a FIFTH stop reason
    (`no_messages`) that SKILL.md does not name.
    """
    page = tgparse.parse_preview(probe("C15-s-durov-q-rare.html"), "durov")
    assert len(page.messages) == 7           # short of PAGE_SIZE: this is the end
    assert page.before is None
    assert page.before_is_fallback is False


def test_paged_page_uses_the_rel_prev_cursor(probe):
    page = tgparse.parse_preview(probe("A01-s-durov.html"), "durov")
    assert page.before == 523
    assert page.after is None
    assert page.before_is_fallback is False


def test_min_id_cursor_fallback_is_live_code(probe):
    # Guards the mutation that exposed it: deleting the fallback left the suite
    # green, because rel="canonical" always supplied a number first. C03 is a
    # FULL page (20 messages), which is the only case the fallback still covers:
    # a page that is full and publishes no cursor is genuinely ambiguous, and
    # truncating a walk there is worse than one wasted request.
    body = probe("C03-s-durov-q.html")
    assert tgparse.parse_preview(body, "durov").before == 517
    stripped = body.replace('rel="prev"', 'rel="notprev"')
    stripped = stripped.replace("tme_messages_more", "tme_messages_gone")
    fallback = tgparse.parse_preview(stripped, "durov")
    assert len(fallback.messages) == tgparse.PAGE_SIZE
    assert fallback.before == min(m.id for m in fallback.messages) == 517
    assert fallback.before_is_fallback is True


def test_forward_paging_reads_the_rel_next_link(probe):
    """`after` is parsed, documented in the selector table and called by nothing.

    No probe carries a `rel="next"` link, so until now the first run that needed
    forward paging would have been its first test. It is kept rather than
    deleted -- it is the only way to read forward from a stored cursor, and
    `preview(after=...)` is one transport parameter -- but it is no longer
    untested: the link is planted on a real page, in Telegram's own shape.
    """
    body = probe("A01-s-durov.html")
    assert 'rel="next"' not in body
    forward = body.replace(
        '<link rel="prev"',
        '<link rel="next" href="/s/durov?after=543">\n<link rel="prev"',
        1,
    )
    assert forward != body, "the rel=prev link moved; the fixture needs rereading"
    page = tgparse.parse_preview(forward, "durov")
    assert page.after == 543
    assert page.before == 523                # and the backward cursor is untouched


# --------------------------------------------------------------------------
# data-peer -- a group's only id on this surface
# --------------------------------------------------------------------------
def test_group_embed_carries_a_chat_peer(probe):
    m = tgparse.parse_embed(probe("C16-embed-birding-1000.html"), "birding_chats", 1000)
    assert m.chat_id is None                     # no data-view on a group embed
    assert m.chat_peer == "c1000000001_4000000000000000001"


def test_channel_embed_data_peer_agrees_with_data_view(probe):
    # data-peer's first component and data-view's `c` are the same number under
    # two sign conventions -- an independent second witness for the channel id.
    m = tgparse.parse_embed(probe("C05-embed-durov-523.html"), "durov", 523)
    assert m.chat_id == -1006503122
    assert m.chat_peer.startswith("c1006503122_")


# --------------------------------------------------------------------------
# a zero-hit search is not an empty page
# --------------------------------------------------------------------------
def test_zero_hit_search_page_sets_found_nothing():
    body = NO_HITS_PAGE.read_text(encoding="utf-8", errors="replace")
    page = tgparse.parse_preview(body, "durov", found_by="zzqwxnonexistentterm12345")
    assert page.found_nothing is True
    assert page.messages == []
    assert page.understood_nothing is False       # nothing to understand, and it says so


# --------------------------------------------------------------------------
# "nothing was said" has to be provable, in both directions
# --------------------------------------------------------------------------
def test_a_post_that_quotes_the_no_results_marker_is_not_a_claim_of_silence(probe):
    """Twenty real posts must never come back as proven silence.

    `found_nothing: true` is what SKILL.md defines as a genuine zero-hit
    search, and it rested on a whole-body substring test. One post containing
    the literal string `tme_no_messages_found` -- and this repair note is
    exactly the sort of text that would -- turned A01's twenty real posts into
    `messages: 0, found_nothing: True`.
    """
    body = probe("A01-s-durov.html")
    start = body.find("js-message_text")
    planted = body.find(">", body.rfind("<div", 0, start))
    poisoned = body[: planted + 1] + tgweb.NO_MESSAGES_FOUND + body[planted + 1 :]
    assert tgweb.NO_MESSAGES_FOUND in poisoned

    page = tgparse.parse_preview(poisoned, "durov")
    assert page.found_nothing is False
    assert len(page.messages) == 20
    assert tgweb.search_found_nothing(poisoned) is False


def test_a_page_whose_blocks_stop_parsing_never_looks_like_an_empty_page(probe):
    """The mutation a Telegram front-end change would actually be.

    Rename `data-post` and the page still has twenty message blocks on it, a
    live `before` cursor and `preview_available() -> True`. It used to parse as
    `messages: [], found_nothing: False` -- byte-identical to a channel that
    simply had nothing to say -- so `search` reported "no hits" and `history`
    spent its whole 25-page ceiling harvesting nothing.
    """
    body = probe("A01-s-durov.html")
    clean = tgparse.parse_preview(body, "durov")
    assert clean.blocks_seen == 20 and clean.blocks_unparsed == 0
    assert clean.understood_nothing is False

    broken = tgparse.parse_preview(body.replace("data-post=", "data-postid="), "durov")
    assert broken.messages == []
    assert broken.blocks_seen == 20
    assert broken.blocks_unparsed == 20
    assert broken.understood_nothing is True, (
        "a parse that understood nothing must not be reportable as absence"
    )
    assert broken.found_nothing is False


def test_understood_nothing_is_said_out_loud(probe, capsys):
    # Fields nobody reads are not a signal. The parser also says it on stderr,
    # because this is a front-end change and the run it happens in is paid for.
    tgparse.parse_preview(
        probe("A01-s-durov.html").replace("data-post=", "data-postid="), "durov"
    )
    assert "not one of them parsed" in capsys.readouterr().err


def test_an_empty_page_with_no_blocks_is_not_a_failed_parse():
    # The other direction: nothing on the page means nothing to understand.
    page = tgparse.parse_preview("<html><body></body></html>", "durov")
    assert page.blocks_seen == 0
    assert page.understood_nothing is False


# --------------------------------------------------------------------------
# ?embed=1 -- no ghosts, and no live post dropped
# --------------------------------------------------------------------------
def test_a_localised_post_not_found_page_never_becomes_a_message(probe):
    """The ghost-post branch, driven by the change that would produce it.

    `parse_embed` used to manufacture a `Message` out of the requested id
    whenever it found no `data-post`. The only pages in the corpus that reach
    that branch are the 7 "Post not found" error pages, so one wording change --
    a localisation, and the request sends `Accept-Language: en,ru;q=0.9` -- made
    every empty id a hit: `--count 50` would fill with 50 fabricated posts and
    the report would say 50 posts were found on a group that said nothing.
    """
    body = probe("C08-embed-tdlibchat-50000.html")
    assert tgweb.post_missing(body) is True
    assert tgparse.parse_embed(body, "tdlibchat", 50000) is None

    localised = body.replace(tgweb.POST_NOT_FOUND, "Сообщение не найдено")
    assert tgweb.POST_NOT_FOUND not in localised
    assert tgweb.post_missing(localised) is True, "the structural marker is the test"
    assert tgparse.parse_embed(localised, "tdlibchat", 50000) is None


def test_a_post_that_quotes_post_not_found_is_still_a_post(probe):
    """The same substring test in the other direction, and the costlier one.

    No `?embed=1` page carries `tgme_widget_message_wrap` -- not the 9 with a
    message, not the 7 with an error -- so the old `and MSG_WRAP not in body`
    guard was always true and the test collapsed to a substring search of the
    post's own text. An ordinary English sentence in a developer group made a
    live message read as an empty id, after the run had paid for the request.
    """
    body = probe("C10-embed-tdlibchat-10000.html")
    assert tgweb.MSG_WRAP not in body
    poisoned = body.replace(
        "Default group permissions",
        "the widget answers Post not found when you delete it",
        1,
    )
    assert poisoned != body, "the fixture text moved; it needs rereading"
    assert tgweb.POST_NOT_FOUND in poisoned
    assert tgweb.post_missing(poisoned) is False
    msg = tgparse.parse_embed(poisoned, "tdlibchat", 10000)
    assert msg is not None and msg.id == 10000


def test_an_unreadable_embed_is_not_an_empty_id(probe, capsys):
    """A join wall, an interstitial or a front-end change is not a deletion.

    Returning None for it is unavoidable -- there is no message to return --
    but it must not pass in silence, because `walk_group` charges the miss
    counter for every None and a run of them ends the walk with "the history
    stops here" about a group that is still talking.
    """
    wall = "<html><body><div class='joinwall'>Join this group to see it</div></body></html>"
    assert tgweb.embed_unreadable(wall) is True
    assert tgparse.parse_embed(wall, "birding_chats", 29327) is None
    assert "neither a message nor" in capsys.readouterr().err
    # and a real page is not confused with one
    assert tgweb.embed_unreadable(probe("C26-embed-birding-29327.html")) is False
    assert tgweb.embed_unreadable(probe("C08-embed-tdlibchat-50000.html")) is False


def test_parse_embed_records_that_it_got_a_different_post(probe):
    """`embed()` follows redirects and nothing compared what came back.

    The record is internally consistent -- id, permalink and text all come off
    the same `data-post` -- which is exactly why this was invisible: the walk
    that asked for id N booked a hit for N and filed a post with another id.
    """
    body = probe("C16-embed-birding-1000.html")
    asked = tgparse.parse_embed(body, "birding_chats", 1000)
    assert asked.id == 1000 and asked.requested_id is None

    served = tgparse.parse_embed(body, "birding_chats", 999999)
    assert served.id == 1000                       # nothing is dropped
    assert served.requested_id == 999999           # and nothing is hidden


def test_decode_data_view_rejects_json_that_is_not_an_object():
    # json.loads returns these happily; both call sites then do .get("c") on the
    # result and one bad attribute took down the parse of the whole page.
    for literal in ("12345", '"a string"', "[1,2,3]", "true", "null"):
        encoded = base64.urlsafe_b64encode(literal.encode()).decode("ascii").rstrip("=")
        assert tgparse.decode_data_view(encoded) is None, literal


def test_a_broken_data_view_does_not_take_down_the_page(probe):
    body = probe("A01-s-durov.html")
    broken = re.sub(r'data-view="[^"]*"',
                    'data-view="' + base64.urlsafe_b64encode(b"12345").decode().rstrip("=") + '"',
                    body)
    page = tgparse.parse_preview(broken, "durov")     # used to raise AttributeError
    assert len(page.messages) == 20
    assert page.chat_id is None


def test_embed_post_not_found_returns_none(probe):
    assert tgparse.parse_embed(probe("C26-embed-birding-29320.html"), "birding_chats", 29320) is None


def test_embed_live_group_message_no_username(probe):
    # C26-29327: the author carries no public username at all -- author
    # identity on this surface is display name always, username sometimes,
    # user id never (tgparse.py module docstring).
    m = tgparse.parse_embed(probe("C26-embed-birding-29327.html"), "birding_chats", 29327)
    assert m is not None
    assert m.author_name == "Author Five"
    assert m.author_username is None
    assert m.text == "Hi"


# --------------------------------------------------------------------------
# decode_data_view -- round trip
# --------------------------------------------------------------------------
def test_decode_data_view_round_trips_the_documented_payload():
    # The exact payload decoded from C05-embed-durov-523.html's data-view
    # attribute: {"c":-1006503122,"p":523,"t":1787431625,"h":"f028d4774a108a2d46"}
    payload = {"c": -1006503122, "p": 523, "t": 1787431625, "h": "f028d4774a108a2d46"}
    raw = json.dumps(payload).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    assert tgparse.decode_data_view(encoded) == payload


def test_decode_data_view_against_the_real_probe(probe):
    import re

    body = probe("C05-embed-durov-523.html")
    m = re.search(r'data-view="([^"]+)"', body)
    assert m is not None
    payload = tgparse.decode_data_view(m.group(1))
    assert payload == {"c": -1006503122, "p": 523, "t": 1787431625, "h": "f028d4774a108a2d46"}


def test_decode_data_view_on_garbage_returns_none():
    assert tgparse.decode_data_view("") is None
    assert tgparse.decode_data_view("not-valid-base64-json!!!") is None


# --------------------------------------------------------------------------
# parse_rounded_count
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("12.5M", 12_500_000),
        ("55.2K", 55_200),
        ("220", 220),
        ("1.11K", 1_110),
        ("", None),
        (None, None),
        # Inputs the old regex admitted and float() then rejected, raising
        # ValueError out of a whole page parse.
        ("1.2.3", None),
        (".", None),
        ("1..2K", None),
        # The views node has a sibling <span class="copyonly"> views</span> in
        # the same meta block. This used to come back as 24 -- the multiplier
        # was dropped and nothing said so.
        ("24M views", 24_000_000),
        ("1.2K views", 1_200),
        # Telegram writes thousands with a non-breaking space.
        ("11 110 268", 11_110_268),
        # A comma is the DECIMAL separator in Russian, German and French, and
        # Russian-language sources are what this skill is for. Stripping every
        # comma read this as 125 000 000 -- ten times the real figure, in the
        # field a report quotes, with nothing anywhere saying so.
        ("12,5M", 12_500_000),
        ("0,5M", 500_000),
        ("1,11K", 1_110),
        # Three digits after the comma is grouping, and stays grouping.
        ("1,234", 1_234),
        ("1,234,567", 1_234_567),
        ("12,345K", 12_345_000),
        # Routing a plain integer through float() cost precision above 2**53:
        # this used to come back one larger than it went in.
        ("999999999999999999999", 999_999_999_999_999_999_999),
    ],
)
def test_parse_rounded_count(raw, expected):
    assert tgparse.parse_rounded_count(raw) == expected


def test_parse_rounded_count_never_raises():
    for raw in ("1.2.3", ".", "1..2K", "...", "K", "-", "12.5.7M", "abc",
                ",", ",,", "1,2,3", ",5M", "1,", "12,5,7M", "1,23,456"):
        tgparse.parse_rounded_count(raw)       # the assertion is the absence of a raise


# --------------------------------------------------------------------------
# No parsed Message ever carries a sender user id
# --------------------------------------------------------------------------
def test_message_dataclass_never_carries_a_user_id_field():
    field_names = [f.name for f in dataclasses.fields(tgparse.Message)]
    import re

    offenders = [n for n in field_names if re.search(r"user.?id", n, re.I)]
    assert offenders == [], f"Message carries a user-id-shaped field: {offenders}"


def test_real_parsed_messages_never_carry_a_user_id_key(probe):
    import re

    sources = [
        ("A01-s-durov.html", "preview", "durov"),
        ("C10-embed-tdlibchat-10000.html", "embed", "tdlibchat"),
        ("C26-embed-birding-29327.html", "embed", "birding_chats"),
    ]
    for name, kind, username in sources:
        body = probe(name)
        if kind == "preview":
            msgs = tgparse.parse_preview(body, username).messages
        else:
            msg = tgparse.parse_embed(body, username, 1)
            msgs = [msg] if msg else []
        for msg in msgs:
            keys = msg.as_dict().keys()
            offenders = [k for k in keys if re.search(r"user.?id", k, re.I)]
            assert offenders == []


# --------------------------------------------------------------------------
# Landing -> PeerCard
# --------------------------------------------------------------------------
def test_parse_landing_channel_durov(probe):
    card = tgparse.parse_landing(probe("C01-landing-durov.html"), "durov")
    assert card.exists is True
    assert card.type == "channel"
    assert card.members == 11_110_268
    assert card.title == "Pavel Durov"
    assert card.description == "Founder of Telegram."


def test_parse_landing_nonexistent(probe):
    # parse_landing() itself does not blank title/description for a missing
    # name -- that scrubbing is discover.verify()'s job, one layer up. Here it
    # is Telegram's own generic "contact" boilerplate, not real page content.
    card = tgparse.parse_landing(probe("C02-landing-nonexistent.html"), "zzqwxnonexistentchannel12345")
    assert card.exists is False
    assert card.type is None
    assert card.taken is False
    assert card.members is None


# --------------------------------------------------------------------------
# Albums: one block, several ids, and not one of them lost
#
# The corpus cannot reach this shape -- `grep -l grouped` over all 32 probes
# matches only `D08-nontme-docs-botapi.html`, a documentation page -- so the
# album is transplanted onto a real page instead of invented: the markup below
# is the wrapper measured live on
# `t.me/s/nexta_tv` on 2026-08-25 (ids 27033-27052 under 18 `data-post`
# attributes, with 27043 and 27044 present only as `?single` hrefs inside the
# group), and the ids it carries are two real ids cut out of A01.
# --------------------------------------------------------------------------
WRAP_START = '<div class="tgme_widget_message_wrap'


def _album_markup(username: str, ids: list[int]) -> str:
    items = "".join(
        '<a class="tgme_widget_message_photo_wrap grouped_media_wrap blured '
        'js-message_photo" style="left:0px;top:0px;width:453px;height:259px;'
        "background-image:url('https://cdn4.telesco.pe/file/album{n}.jpg')\" "
        'data-ratio="1.746" href="https://t.me/{u}/{n}?single">'
        '<div class="grouped_media_helper">'
        '<div class="tgme_widget_message_photo grouped_media"></div></div></a>'.format(n=n, u=username)
        for n in ids
    )
    return (
        '<div class="tgme_widget_message_grouped_wrap js-message_grouped_wrap" '
        'data-margin-w="2" data-margin-h="2" style="width:453px;">'
        '<div class="tgme_widget_message_grouped js-message_grouped" '
        'style="padding-top:79.912%">'
        '<div class="tgme_widget_message_grouped_layer js-message_grouped_layer" '
        'style="width:453px;height:362px">' + items + "</div></div></div>"
    )


def _page_with_an_album(body: str, username: str = "durov", cut_at: int = 1) -> tuple:
    """A01 rebuilt into the shape a channel that posts albums serves.

    Two message blocks are cut out and their ids are given to an album planted
    inside the first block still standing, so the page accounts for exactly the
    twenty ids it did before -- under eighteen `data-post` attributes, which is
    what nexta_tv served. `cut_at` decides whether the album's other ids sit
    above the block's own id (as they did live) or below it.
    """
    starts = [m.start() for m in re.finditer(re.escape(WRAP_START), body)]
    assert len(starts) == 20, "A01 no longer carries twenty blocks"
    swallowed = [int(n) for n in
                 re.findall(r'data-post="%s/(\d+)"' % username,
                            body[starts[cut_at]:starts[cut_at + 2]])]
    assert len(swallowed) == 2, swallowed
    rest = body[:starts[cut_at]] + body[starts[cut_at + 2]:]
    opened = rest.index(">", rest.index(WRAP_START)) + 1
    return rest[:opened] + _album_markup(username, swallowed) + rest[opened:], swallowed


def test_an_album_block_accounts_for_every_id_it_carried(probe):
    """Twenty posts came back as eighteen records and nothing said so.

    Telegram renders grouped media as ONE `tgme_widget_message_wrap` with ONE
    `data-post`, so `blocks_seen` was 18, `blocks_unparsed` was 0 and two ids
    existed nowhere in the output. Measured live on t.me/s/nexta_tv: ids
    27043 and 27044 were inside the block whose `data-post` is nexta_tv/27042,
    and a `?q=` hit whose caption lives on one of them came back permalinked to
    27042 -- a link that resolves, to the wrong message.
    """
    body = probe("A01-s-durov.html")
    every_id = sorted(m.id for m in tgparse.parse_preview(body, "durov").messages)
    with_album, swallowed = _page_with_an_album(body)

    page = tgparse.parse_preview(with_album, "durov")
    assert page.blocks_seen == 18                 # the page LOOKS two posts short
    assert len(page.messages) == 18
    album = min(page.messages, key=lambda m: m.id)
    assert album.ids == sorted([album.id] + swallowed)
    assert page.ids_seen == 20                    # and it was not
    assert sorted(i for m in page.messages for i in m.ids) == every_id


def test_a_lone_post_carries_exactly_its_own_id(probe):
    page = tgparse.parse_preview(probe("A01-s-durov.html"), "durov")
    assert all(m.ids == [m.id] for m in page.messages)
    assert page.ids_seen == len(page.messages) == 20
    single = tgparse.parse_embed(probe("C05-embed-durov-523.html"), "durov", 523)
    assert single.ids == [523]


def test_a_page_carrying_an_album_is_still_a_full_page(probe):
    """`len(page.messages) >= PAGE_SIZE` can never be true on such a channel.

    The min-id fallback is the cursor a page that publishes no `?before=` link
    of its own gets, and it is gated on the page being full. Counted in parsed
    messages that gate was unreachable on any channel that posts albums -- and
    by its own reasoning ("a page that short is the last one") a full page would
    have ended the walk.
    """
    with_album, _ = _page_with_an_album(probe("A01-s-durov.html"))
    stripped = with_album.replace('rel="prev"', 'rel="notprev"')
    stripped = stripped.replace("tme_messages_more", "tme_messages_gone")

    page = tgparse.parse_preview(stripped, "durov")
    assert len(page.messages) == 18 < tgparse.PAGE_SIZE
    assert page.ids_seen == tgparse.PAGE_SIZE
    assert page.is_full is True
    assert page.before == 523
    assert page.before_is_fallback is True


def test_a_page_whose_blocks_all_fail_to_parse_is_not_a_short_page(probe):
    """The same gate, from the other side: a partial parse failure is not an end.

    Half the blocks failing to yield a record used to shorten the page below
    PAGE_SIZE and so convert a broken parse into "this was the last page".
    """
    body = probe("C03-s-durov-q.html").replace('rel="prev"', 'rel="notprev"')
    body = body.replace("tme_messages_more", "tme_messages_gone")
    starts = [m.start() for m in re.finditer(re.escape(WRAP_START), body)]
    broken = body[:starts[10]] + body[starts[10]:].replace("data-post=", "data-postid=")

    page = tgparse.parse_preview(broken, "durov")
    assert page.blocks_seen == 20
    assert page.blocks_unparsed == 10
    assert len(page.messages) == 10 < tgparse.PAGE_SIZE
    assert page.is_full is True
    assert page.before_is_fallback is True and page.before is not None


# --------------------------------------------------------------------------
# A full page that says nothing at all is a broken selector, not a silent channel
# --------------------------------------------------------------------------
def test_a_full_page_with_no_text_on_it_is_a_front_end_change(probe, capsys):
    """`js-message_text` is a JS hook class -- the most rot-prone value in SEL.

    Rename it and every block still parses off `data-post`, so `blocks_unparsed`
    is 0 and `understood_nothing` was False: twenty posts with `text: ""` went
    out as a result and a report built on them quoted nothing from any of them.
    Measured on six pages, corpus and live, on 2026-08-25: pristine, every one
    has text on all 20 messages; renamed, every one has text on none.
    """
    body = probe("A01-s-durov.html")
    clean = tgparse.parse_preview(body, "durov")
    assert clean.understood_nothing is False
    assert clean.no_message_carries_text is False

    broken = tgparse.parse_preview(body.replace(tgparse.SEL["msg_text"], "x"), "durov")
    assert len(broken.messages) == 20         # every block parsed...
    assert broken.blocks_unparsed == 0        # ...and the old signal saw nothing
    assert [m.text for m in broken.messages] == [""] * 20
    assert broken.understood_nothing is True
    assert broken.found_nothing is False
    assert "text selector" in capsys.readouterr().err


def test_a_page_of_captionless_media_is_not_accused_of_being_broken(probe):
    """The false positive this must not have, and the price it pays for that.

    `understood_nothing` stops a walk, so a page of caption-less photos must
    never trip it. Only a FULL page may make the accusation -- which is also
    why a rename would go unnoticed on the last page of a walk or on a small
    `?q=` result, and that is the deliberate direction of the trade.
    """
    short = probe("C15-s-durov-q-rare.html").replace(tgparse.SEL["msg_text"], "x")
    page = tgparse.parse_preview(short, "durov")
    assert len(page.messages) == 7
    assert page.is_full is False
    assert page.no_message_carries_text is False
    assert page.understood_nothing is False


def test_one_post_with_text_is_enough_to_clear_the_page(probe):
    body = probe("A01-s-durov.html")
    broken = body.replace(tgparse.SEL["msg_text"], "x", 19)   # 20 blocks, one spared
    page = tgparse.parse_preview(broken, "durov")
    assert sum(1 for m in page.messages if m.text) >= 1
    assert page.understood_nothing is False


# --------------------------------------------------------------------------
# a page carrying posts can never claim its own silence, at the parse layer
# --------------------------------------------------------------------------
def test_the_zero_hit_marker_as_a_real_element_cannot_silence_a_full_page(probe):
    """The half of `search_found_nothing` no test covered.

    `test_search_found_nothing_is_not_a_substring_search` plants the marker into
    a post's TEXT, where `_has_class` never matches, so it passes with or
    without the `data-post` guard. Planted as an element's CLASS on a page that
    also carries messages -- the only input that separates the two halves --
    deleting the guard made A01's twenty real posts come back as
    `messages: 0, found_nothing: True`, i.e. as proven silence.
    """
    body = probe("A01-s-durov.html")
    poisoned = body.replace(
        "</body>",
        '<div class="tgme_widget_message_centered">'
        '<div class="%s">No posts found</div></div></body>' % tgweb.NO_MESSAGES_FOUND,
    )
    assert poisoned != body
    assert tgweb._has_class(poisoned, tgweb.NO_MESSAGES_FOUND) is True
    assert tgweb.DATA_POST in poisoned

    page = tgparse.parse_preview(poisoned, "durov")
    assert page.found_nothing is False
    assert len(page.messages) == 20
    assert page.blocks_seen == 20


# --------------------------------------------------------------------------
# links inside post text
# --------------------------------------------------------------------------
def test_a_link_inside_a_post_keeps_its_destination(probe):
    """`text` keeps the anchor's words and used to drop the URL, always.

    Measured 2026-08-25 on two live pages: durov 21 of 21 anchors and rian_ru
    41 of 41 had a destination that was not recoverable from the text. On
    rian_ru/344764 the lost href was the news story the post reports.
    """
    page = tgparse.parse_preview(probe("A01-s-durov.html"), "durov")
    linked = [m for m in page.messages if m.links]
    assert linked, "A01 carries formatted links; the fixture needs rereading"
    for msg in linked:
        for link in msg.links:
            assert link["href"].startswith(("http://", "https://", "tg://", "/")), link
            assert set(link) == {"text", "href"}
    # and the text is untouched by the extraction
    plain = tgparse.parse_preview(probe("A01-s-durov.html"), "durov")
    assert [m.text for m in plain.messages] == [m.text for m in page.messages]


def test_a_link_whose_words_are_not_its_url_is_recoverable():
    """The shape `discover --found-via link` is documented to mine.

    A `t.me` link whose anchor text is prose ("Подписаться на канал")
    survived as prose alone, so the documented discovery channel was fed a
    stream the usernames had already been deleted from -- on a Russian channel
    it could not produce a single candidate.

    The markup below is synthetic: the shape is what a news channel serves, the
    words and the destination are invented.
    """
    html = (
        '<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" '
        'data-post="newschannel/344764"><div class="tgme_widget_message_text '
        'js-message_text">Городской совет '
        '<a href="https://example.invalid/2026/transport-plan">утвердил</a> план'
        '<br><a href="https://t.me/newschannel">Подписаться на канал</a>'
        "</div></div></div>"
    )
    page = tgparse.parse_preview(html, "newschannel")
    msg = page.messages[0]
    assert "утвердил" in msg.text and "example.invalid" not in msg.text
    assert msg.links == [
        {"text": "утвердил", "href": "https://example.invalid/2026/transport-plan"},
        {"text": "Подписаться на канал", "href": "https://t.me/newschannel"},
    ]


def test_an_anchor_with_no_href_is_not_a_link():
    html = ('<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" '
            'data-post="x/1"><div class="js-message_text">see <a>this</a></div>'
            "</div></div>")
    assert tgparse.parse_preview(html, "x").messages[0].links == []


# --------------------------------------------------------------------------
# media_urls: the first entry of a video post is a poster
# --------------------------------------------------------------------------
def test_a_video_posts_first_media_url_is_a_poster_not_the_file(probe):
    """The docstring promised "real files, not thumbnails ... they carry a token".

    Both halves are false for `media_urls[0]` of a video: it is the still out of
    `tgme_widget_message_video_thumb`'s background-image, it carries no `token=`,
    and a caller downloading it for a record whose `media` says `['video']` gets
    a JPEG.
    """
    page = tgparse.parse_preview(probe("A01-s-durov.html"), "durov")
    video = min(page.messages, key=lambda m: m.id)
    assert video.media == ["video", "unsupported:video"]
    assert len(video.media_urls) == 2
    assert video.media_posters == [video.media_urls[0]]
    assert "token=" not in video.media_urls[0]
    assert "token=" in video.media_urls[1]


def test_a_photo_is_the_file_even_though_it_is_a_css_background(probe):
    # The other direction, and why the test is the poster class rather than
    # "did this URL come out of a style attribute": a photo post's image IS a
    # background-image and it IS the file.
    page = tgparse.parse_preview(probe("A09-s-Astana_motoriders.html"), "Astana_motoriders")
    photos = [m for m in page.messages if m.media == ["photo"] and m.media_urls]
    assert photos, "A09 carries photo posts; the fixture needs rereading"
    assert all(m.media_posters == [] for m in photos)


def test_no_message_in_the_corpus_calls_a_real_file_a_poster(fixtures):
    for name, msg in corpus_messages(fixtures):
        assert set(msg.media_posters) <= set(msg.media_urls), (name, msg.id)
        for url in msg.media_posters:
            assert "token=" not in url, (name, msg.id, url)


# --------------------------------------------------------------------------
# is_service_message: the /s/ marker on the wrap itself
# --------------------------------------------------------------------------
def test_a_service_class_on_the_wrap_itself_is_a_service_message():
    """Half of `is_service_message` had no input in the corpus that reached it.

    Every real page puts `service_message` on the `tgme_widget_message` INSIDE
    the wrap, so `if wrap.has_class(...)` could be deleted outright with all 32
    probes byte-identical and the whole suite green. On `?embed=1` the
    `tgme_widget_message` div IS the wrap, so if Telegram ever serves the `/s/`
    marker there, a pin or a join event enters posts.jsonl as a quotable post.
    Hand-built because no saved page has this shape -- the assertion is about a
    branch, not about what Telegram serves today.
    """
    inner = ('<div class="tgme_widget_message js-widget_message" data-post="x/1">'
             '<div class="tgme_widget_message_text js-message_text">pinned</div></div>')
    marked = tgdom.parse(
        '<div class="tgme_widget_message_wrap %s">%s</div>'
        % (tgparse.SEL["service"], inner)
    ).find(cls=tgparse.SEL["msg_wrap"])
    plain = tgdom.parse(
        '<div class="tgme_widget_message_wrap">%s</div>' % inner
    ).find(cls=tgparse.SEL["msg_wrap"])

    assert tgparse.is_service_message(marked) is True
    assert tgparse.is_service_message(plain) is False


def test_a_recognised_missing_post_says_nothing_on_stderr(probe, capsys):
    """The alarm that means "Telegram changed" must not fire on an ordinary gap.

    Deleting `parse_embed`'s `post_missing` guard leaves the return value None
    either way -- the fallback catches it -- but every empty id then shouts the
    front-end-change warning. At the 1.7 % head density measured on a group,
    `--count 50` fires it on ~98 % of the ids it tries, and the one alarm that
    matters becomes noise nobody reads.
    """
    for name in ("C26-embed-birding-29320.html", "C08-embed-tdlibchat-50000.html"):
        capsys.readouterr()
        assert tgparse.parse_embed(probe(name), "birding_chats", 29320) is None
        assert capsys.readouterr().err == "", name


def test_a_data_post_id_too_long_to_be_an_id_is_not_a_crash(probe):
    """`int()` refuses a 4301-digit string, and the ValueError came out of a
    public entry point on a body that arrived from the network."""
    body = probe("A01-s-durov.html").replace(
        'data-post="durov/523"', 'data-post="durov/' + "9" * 5000 + '"'
    )
    page = tgparse.parse_preview(body, "durov")      # used to raise ValueError
    assert len(page.messages) == 19
    assert page.blocks_unparsed == 1
    assert 523 not in [m.id for m in page.messages]


def test_an_album_never_claims_an_id_from_another_channel():
    """A grouped block links to its own items and to nothing else.

    If it ever links elsewhere, that post is somebody else's and this page did
    not serve it -- counting it would inflate `ids_seen`, which is what decides
    "was this page full" and how many posts a run reports.
    """
    inner = (
        '<div class="tgme_widget_message" data-post="durov/523">'
        '<div class="tgme_widget_message_grouped_wrap js-message_grouped_wrap">'
        '<a href="https://t.me/durov/524?single">a</a>'
        '<a href="https://t.me/someoneelse/999?single">b</a>'
        '<a href="https://t.me/durov/525">c</a>'          # no ?single: the block itself
        "</div></div>"
    )
    page = tgparse.parse_preview(
        '<div class="tgme_widget_message_wrap">%s</div>' % inner, "durov"
    )
    assert page.messages[0].ids == [523, 524]
    assert page.ids_seen == 2


def test_the_cursor_is_the_smallest_id_the_page_accounted_for(probe):
    """Not the smallest `data-post` -- the smallest id, album members included.

    Live nexta_tv served the album under its own lowest id, so the two readings
    agreed there and a test built only on that page cannot tell them apart. The
    contract is the one that does not depend on which item of a group Telegram
    picks for `data-post`: `?before=` is exclusive, so the next page must start
    below the OLDEST post this page displayed, whichever block carried it.
    """
    body = probe("A01-s-durov.html")
    with_album, swallowed = _page_with_an_album(body, cut_at=0)
    assert swallowed == [523, 524]

    stripped = with_album.replace('rel="prev"', 'rel="notprev"')
    stripped = stripped.replace("tme_messages_more", "tme_messages_gone")
    page = tgparse.parse_preview(stripped, "durov")

    host = min(page.messages, key=lambda m: m.id)
    assert host.id == 525                       # the album's own data-post...
    assert host.ids == [523, 524, 525]          # ...is not its smallest id
    assert min(m.id for m in page.messages) == 525
    assert page.ids_seen == 20
    assert page.before == 523
    assert page.before_is_fallback is True


# --------------------------------------------------------------------------
# The album href, in the three forms a page may serve it in
# --------------------------------------------------------------------------
def _album_block(username: str, mid: int, hrefs: list[str]) -> str:
    """One message block whose grouped wrapper carries exactly these hrefs."""
    items = "".join('<a href="%s">x</a>' % href for href in hrefs)
    return (
        '<div class="tgme_widget_message_wrap">'
        '<div class="tgme_widget_message" data-post="%s/%d">'
        '<div class="tgme_widget_message_grouped_wrap js-message_grouped_wrap">'
        "%s</div></div></div>" % (username, mid, items)
    )


@pytest.mark.parametrize("href", [
    "https://t.me/durov/524?single",
    "http://t.me/durov/524?single",
    "//t.me/durov/524?single",          # protocol-relative
    "/durov/524?single",                # site-relative
])
def test_an_album_id_is_found_whichever_form_the_link_takes(href):
    """Only the absolute form was matched, and the loss was silent.

    A relative or protocol-relative `?single` href is the same link to the same
    item, and dropping it drops an id that exists nowhere else in the markup --
    with `blocks_unparsed` at 0, because the block itself parsed perfectly.
    This branch has no fixture in the corpus (`grep -l grouped` matches one
    documentation page), which is exactly why the parser must accept every form
    the surface is free to serve rather than the one that was seen once.
    """
    page = tgparse.parse_preview(_album_block("durov", 523, [href]), "durov")
    assert page.messages[0].ids == [523, 524]
    assert page.ids_seen == 2


def test_an_album_id_survives_the_channel_name_in_another_case():
    """`BirdingChats` and `birding_chats`' neighbours are one peer, not two.

    The name in the href is whatever case the link was written in, and the
    comparison was exact -- so a page that capitalised its own name lost every
    album id it carried.
    """
    page = tgparse.parse_preview(
        _album_block("durov", 523, ["https://t.me/DuRoV/524?single"]), "durov"
    )
    assert page.messages[0].ids == [523, 524]


def test_a_relative_href_to_another_channel_is_still_refused():
    # The other half of the rule: only ids under the same username are taken.
    page = tgparse.parse_preview(
        _album_block("durov", 523, ["/someoneelse/999?single",
                                    "https://example.com/durov/777?single"]),
        "durov",
    )
    assert page.messages[0].ids == [523]


# --------------------------------------------------------------------------
# A full page of caption-less media is a channel that posts pictures
# --------------------------------------------------------------------------
def test_a_full_page_of_captionless_media_is_not_a_front_end_change(probe):
    """`understood_nothing` stops a walk and throws the page away.

    A page of twenty photos with no captions carries no text and never did, and
    it used to come back `understood_nothing: true` -- the walk stopped, the
    twenty correctly parsed posts were discarded, and the run reported a
    front-end change that had not happened. The mutation below is A01 with its
    text selector renamed AND every message given a photo, which is the shape
    such a page has: every block yielded something.
    """
    body = probe("A01-s-durov.html")
    media_everywhere = (body.replace(tgparse.SEL["msg_text"], "x")
                            .replace(tgparse.SEL["bubble"], tgparse.SEL["photo"]))
    page = tgparse.parse_preview(media_everywhere, "durov")

    assert len(page.messages) == 20
    assert page.is_full is True
    assert [m.text for m in page.messages] == [""] * 20
    assert all(m.media for m in page.messages)
    assert page.no_message_carries_text is False
    assert page.understood_nothing is False


def test_a_page_where_a_block_yielded_nothing_at_all_is_still_a_change(probe):
    """The other side of the same test, and why it is `all` and not `any`.

    When the text selector moves, the posts that carried media still have it --
    A01 renamed is 8 media posts and 12 records with nothing in them -- so a
    single photo on the page would have been enough to wave the rename through
    if media were merely counted somewhere on the page.
    """
    broken = tgparse.parse_preview(
        probe("A01-s-durov.html").replace(tgparse.SEL["msg_text"], "x"), "durov"
    )
    assert sum(1 for m in broken.messages if m.media) == 8      # not zero
    assert sum(1 for m in broken.messages if not m.media) == 12
    assert broken.understood_nothing is True


# --------------------------------------------------------------------------
# small parsers: a grouped count, and a CSS url with a bracket in it
# --------------------------------------------------------------------------
def test_a_reaction_count_may_carry_a_space_inside_the_number():
    """Telegram groups thousands with a narrow no-break space.

    `👍 1 234` matched nothing, so the whole label became the key and the count
    came out empty: the reaction was stored as `{"👍 1 234": ""}` -- a key no
    caller can compare and a count nobody can read.
    """
    block = (
        '<div class="tgme_widget_message_wrap">'
        '<div class="tgme_widget_message" data-post="durov/523">'
        '<div class="tgme_widget_message_reactions">'
        '<span class="tgme_reaction">\U0001F44D 1\u202f234</span>'
        '<span class="tgme_reaction">\U0001F525 7</span>'
        "</div></div></div>"
    )
    page = tgparse.parse_preview(block, "durov")
    assert page.messages[0].reactions == {
        "\U0001F44D": "1\u202f234",
        "\U0001F525": "7",
    }


def test_a_media_url_is_not_cut_at_the_first_bracket_inside_it():
    r"""`url\(...)` stopped at the first `)` inside the value, quoted or not.

    Half a URL downloads nothing while looking, in the record, exactly like a
    file that was captured.
    """
    inner = (
        '<div class="tgme_widget_message" data-post="durov/523">'
        '<a class="tgme_widget_message_photo_wrap" style="background-image:'
        "url('https://cdn4.telesco.pe/file/a(1)b.jpg')\"></a></div>"
    )
    page = tgparse.parse_preview(
        '<div class="tgme_widget_message_wrap">%s</div>' % inner, "durov"
    )
    assert page.messages[0].media_urls == ["https://cdn4.telesco.pe/file/a(1)b.jpg"]
