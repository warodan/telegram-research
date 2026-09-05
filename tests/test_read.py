"""read.py -- the stage 4 orchestrators, driven end to end through FakeWeb.

No live fixture captures a multi-page channel walk or a group with a
controlled gap (the probes are single snapshots, not a crawl), so several
tests here reuse two or more real saved bodies at different mapped URLs --
`read_group_message`/`parse_embed` only look at body content, never at the
URL that was requested, so this is honest reuse, not invented content. Every
number asserted below was produced by running these exact functions against
these exact mappings first.

**The group tests model density, and that is the point of them.** The
previous suite tested `find_max_id` against islands 99.8-100 % occupied, so
every one of them passed while the function returned 7, 41, 67, 92, 549, 680
or nothing at all for a real group whose newest id was 29 327. The generator
below occupies ids at the 1.7 % measured live -- 3 messages across 175
consecutive ids, with a forced 124-id run of empties between two live ones,
exactly as `hanoi_chats` served on 2026-08-24.
"""

from __future__ import annotations

import random

import pytest

import read
import tgparse
import tgweb
from conftest import FakeWeb, embed_url, preview_url


# --------------------------------------------------------------------------
# walk_channel -- follows the before cursor, stops when it stops moving
# --------------------------------------------------------------------------
def test_walk_channel_follows_before_cursor_across_two_pages_then_stops(probe):
    a01 = probe("A01-s-durov.html")
    c15 = probe("C15-s-durov-q-rare.html")
    # Page 1 (before=None) is the real A01 page: 20 messages, real rel="prev"
    # link at 523.
    # Page 2 (before=523) is C15 -- a different real page, reused here purely
    # for its 7-message body: fresh content, proving the cursor from page 1 was
    # actually sent. C15 is a terminal page (no rel="prev", no more-anchor, 7
    # messages of a 20-message page), so `tgparse` publishes no cursor for it
    # and the walk ends there. A third page is mapped and must never be
    # requested: paying a request to be told the next page is empty is what this
    # walk used to do on every channel it read.
    web = FakeWeb({
        preview_url("durov", before=None): {"status": 200, "body": a01},
        preview_url("durov", before=523): {"status": 200, "body": c15},
        preview_url("durov", before=62): {"status": 200, "body": c15},
    })
    result = read.walk_channel(web, "durov", max_pages=10)

    assert web.calls == [
        preview_url("durov", before=None),
        preview_url("durov", before=523),
    ]
    assert preview_url("durov", before=62) not in web.calls
    assert result.requests == 2
    assert result.exhausted is True
    assert result.stopped_early is None
    assert result.stop_reason == "no_more_pages"
    ids = sorted(m.id for m in result.messages)
    assert len(ids) == 27  # 20 from A01 + 7 from C15
    assert ids[0] == 62
    assert ids[-1] == 543


def test_walk_channel_until_id_truncates_correctly(probe):
    a01 = probe("A01-s-durov.html")
    web = FakeWeb({preview_url("durov", before=None): {"status": 200, "body": a01}})
    result = read.walk_channel(web, "durov", until_id=530, max_pages=10)

    assert result.requests == 1  # the stop is decided from page 1 alone
    assert result.exhausted is True
    assert sorted(m.id for m in result.messages) == list(range(531, 544))


def test_until_id_is_not_reported_as_having_reached_the_first_post(probe):
    """One boolean used to answer three different questions.

    `exhausted` was set by `until_id`, by a cursor that stopped moving, and by
    an id at or below 1 -- and `tg.py` printed it as `reached_first_post`. A
    caller deciding "is this channel fully read" got `true` from a walk that
    stopped 530 posts in. @durov has posts down to id 1.
    """
    a01 = probe("A01-s-durov.html")
    web = FakeWeb({preview_url("durov", before=None): {"status": 200, "body": a01}})
    result = read.walk_channel(web, "durov", until_id=530, max_pages=10)

    assert result.reached_until_id is True
    assert result.reached_first_post is False        # nothing proved it
    assert result.stop_reason == "until_id"


def test_a_cursor_that_stops_moving_is_not_proof_of_the_first_post(probe):
    """The second of the three: no further cursor means this surface will
    serve nothing older, which is not the same claim."""
    c15 = probe("C15-s-durov-q-rare.html")
    web = FakeWeb({
        preview_url("durov", before=None): {"status": 200, "body": c15},
        preview_url("durov", before=62): {"status": 200, "body": c15},
    })
    result = read.walk_channel(web, "durov", max_pages=10)

    assert result.no_more_pages is True
    assert result.reached_first_post is False        # oldest id reached was 62
    assert result.exhausted is True
    assert result.stop_reason == "no_more_pages"


def test_a_page_it_could_not_read_stops_the_walk_and_is_not_called_empty(probe):
    """A read that understood nothing is not a read that found nothing.

    First: `min()` over an empty page used to lose the whole walk to a
    traceback. It does not, and must not -- but walking ON was the wrong repair.
    A `/s/` page whose blocks stop yielding `data-post` parses as zero messages
    with a live cursor, which is byte-for-byte the output of a channel with
    nothing to say; the walk then paged through pages it could not read, spent
    the ceiling and reported silence. `tgparse` now counts the blocks it saw
    against the ones it understood, and this is the half that acts on it.

    The fixture is the real A01 page with `data-post` renamed and nothing else
    touched: 20 message blocks, 0 parsed, cursor 523 intact, `preview_available`
    still true. The second page is mapped and must never be fetched.
    """
    a01 = probe("A01-s-durov.html")
    blind_page = a01.replace("data-post=", "data-nopost=")
    web = FakeWeb({
        preview_url("durov", before=None): {"status": 200, "body": blind_page},
        preview_url("durov", before=523): {"status": 200, "body": blind_page},
    })

    result = read.walk_channel(web, "durov", max_pages=10)

    assert result.messages == []
    assert result.requests == 1, "it kept paying for pages it could not read"
    assert result.understood_nothing is True
    assert result.blocks_unparsed == 20
    assert result.stop_reason == "understood_nothing"
    assert result.no_more_pages is False      # the surface published a cursor
    assert result.found_nothing is False      # and it did NOT say it was empty
    assert result.exhausted is False          # nothing ran out of material
    assert result.reached_first_post is False
    assert "front-end change" in result.stopped_early


def test_a_search_page_it_could_not_read_is_not_a_zero_hit_search(probe):
    """`found_nothing` is the promise this one must not be confused with.

    A genuine zero-hit search is a fact about the channel -- Telegram says so in
    the page. A page of hits this skill can no longer parse says nothing about
    the channel at all, and reporting the second as the first is how a report
    comes to state an absence that was never measured.
    """
    c15 = probe("C15-s-durov-q-rare.html")
    blind = c15.replace("data-post=", "data-nopost=")
    web = FakeWeb({preview_url("durov", query="bitcoin", before=None):
                   {"status": 200, "body": blind}})

    result = read.search_channel(web, "durov", "bitcoin", max_pages=5)

    assert result.messages == []
    assert result.understood_nothing is True
    assert result.found_nothing is False
    assert result.stop_reason == "understood_nothing"
    assert result.requests == 1


def test_an_ordinary_page_never_looks_unreadable(probe):
    """The other direction: the guard must not fire on a page that parsed."""
    a01 = probe("A01-s-durov.html")
    web = FakeWeb({preview_url("durov", before=None): {"status": 200, "body": a01}})
    result = read.walk_channel(web, "durov", max_pages=1)

    assert len(result.messages) == 20
    assert result.understood_nothing is False
    assert result.blocks_unparsed == 0
    assert result.stop_reason == "page_ceiling"


def test_walk_channel_302_raises_wrong_route(probe):
    # A group's `/s/` answers 302 to its own landing URL, and the body that
    # comes with it is empty -- measured live, on a saved probe that did not
    # travel with the public copy of the corpus.
    web = FakeWeb({
        preview_url("hanoi_chats", before=None): tgweb.Response(
            url=preview_url("hanoi_chats", before=None),
            status=302,
            body="",
            location="https://t.me/hanoi_chats",
        ),
    })
    with pytest.raises(read.WrongRoute):
        read.walk_channel(web, "hanoi_chats")


def test_search_channel_302_raises_wrong_route():
    web = FakeWeb({
        preview_url("hanoi_chats", query="word"): tgweb.Response(
            url=preview_url("hanoi_chats", query="word"),
            status=302,
            body="",
            location="https://t.me/hanoi_chats",
        ),
    })
    with pytest.raises(read.WrongRoute):
        read.search_channel(web, "hanoi_chats", "word")


# --------------------------------------------------------------------------
# A stop signal must not also destroy the harvest
# --------------------------------------------------------------------------
class AbortingWeb(FakeWeb):
    """A FakeWeb that raises `RunAborted` on the Nth request, as a 429 does."""

    def __init__(self, responses: dict, *, abort_after: int):
        super().__init__(responses)
        self.abort_after = abort_after

    def fetch(self, url: str, *, follow: bool = False, save_as: str | None = None):
        if self.request_count >= self.abort_after:
            raise tgweb.RunAborted("429 from t.me -- the run stops here")
        return super().fetch(url, follow=follow, save_as=save_as)


def test_an_aborted_channel_walk_carries_what_it_already_read(probe):
    """`RunAborted` still propagates -- it now arrives with the pages paid for.

    The stop signal is correct; throwing away 27 posts already fetched on top
    of it is not. The exception is not caught here, so the CLI's existing
    `except tgweb.RunAborted` keeps working unchanged.
    """
    a01 = probe("A01-s-durov.html")
    # C15 is a terminal page: it publishes no cursor, so a walk that reached it
    # would stop there of its own accord and never make the third request this
    # test needs. The one line A01 carries and C15 does not is put back, byte
    # for byte as Telegram writes it, so page 2 is an ordinary middle page.
    c15 = probe("C15-s-durov-q-rare.html").replace(
        '<link rel="canonical" href="/s/durov?q=bitcoin&before=441">',
        '<link rel="prev" href="/s/durov?before=61">'
        '<link rel="canonical" href="/s/durov?q=bitcoin&before=441">',
    )
    assert 'rel="prev"' in c15, "the fixture edit did not land"
    web = AbortingWeb({
        preview_url("durov", before=None): {"status": 200, "body": a01},
        preview_url("durov", before=523): {"status": 200, "body": c15},
        preview_url("durov", before=61): {"status": 200, "body": c15},
    }, abort_after=2)

    with pytest.raises(tgweb.RunAborted) as caught:
        read.walk_channel(web, "durov", max_pages=10)

    partial = getattr(caught.value, "partial", None)
    assert partial is not None, "the harvest was lost with the stop signal"
    assert len(partial.messages) == 27
    assert partial.stop_reason == "aborted"
    assert partial.requests == 2


# --------------------------------------------------------------------------
# search_channel -- a real page whose own "load more" link points at itself
# --------------------------------------------------------------------------
def test_search_channel_real_rare_query(probe):
    """One page of hits costs ONE request, not two.

    C15 is the only page of results (7 hits) and publishes no `rel="prev"` link,
    so it is the last page and says so. While `tgparse` filled that in with the
    smallest id on the page, `page.before` was never None and this walk could
    never take its "no further cursor" branch on the page that carried the
    answer -- it took it one request later, on the empty page after it. That is
    one wasted GET per query per channel, against a `SKILL.md` cost table that
    prices `search` at one GET per page of hits. The empty page below is mapped
    and must never be fetched.
    """
    c15 = probe("C15-s-durov-q-rare.html")
    no_hits = (f'<html><body><div class="{tgweb.NO_MESSAGES_FOUND}">no</div>'
               "</body></html>")
    web = FakeWeb({
        preview_url("durov", query="bitcoin", before=None): {"status": 200, "body": c15},
        preview_url("durov", query="bitcoin", before=62): {"status": 200, "body": no_hits},
    })
    result = read.search_channel(web, "durov", "bitcoin", max_pages=5)

    assert result.requests == 1
    assert preview_url("durov", query="bitcoin", before=62) not in web.calls
    assert result.exhausted is True
    assert result.found_nothing is False
    assert result.stop_reason == "no_more_pages"
    assert sorted(m.id for m in result.messages) == [62, 67, 77, 116, 215, 232, 440]


def test_search_results_carry_the_query_that_found_them(probe):
    """`found_by` could be nulled on every search hit, suite still green.

    `SKILL.md` §5 makes `found_by` the difference between "a `?q=` search
    returned this" and "a walk happened past it". The two tests that looked like
    coverage hand-built a `Message(found_by=...)` and asserted their own input;
    the wiring that puts the value there lives here, on the real parse of a real
    saved page.
    """
    c15 = probe("C15-s-durov-q-rare.html")
    web = FakeWeb({
        preview_url("durov", query="bitcoin", before=None): {"status": 200, "body": c15},
    })
    result = read.search_channel(web, "durov", "bitcoin", max_pages=1)

    assert result.messages, "the fixture stopped parsing; the test proves nothing"
    assert {m.found_by for m in result.messages} == {"bitcoin"}

    # And the other half of the same promise: a walk has no query behind it.
    a01 = probe("A01-s-durov.html")
    walked = read.walk_channel(
        FakeWeb({preview_url("durov", before=None): {"status": 200, "body": a01}}),
        "durov", max_pages=1,
    )
    assert walked.messages
    assert {m.found_by for m in walked.messages} == {None}


def test_a_page_ceiling_of_zero_is_refused_rather_than_answered(probe):
    """`--max-pages 0` returned `found: 0` with zero requests, on both routes.

    `cmd_group` already refuses `--count 0` in as many words -- "a run that
    returns `found: 0` for a group that is full of them is a silence an agent
    would write into a report as a fact". The identical silence was reachable on
    the two channel routes and was not refused.
    """
    web = FakeWeb({})
    for call in (
        lambda: read.search_channel(web, "durov", "bitcoin", max_pages=0),
        lambda: read.walk_channel(web, "durov", max_pages=0),
        lambda: read.search_channel(web, "durov", "bitcoin", max_pages=-3),
    ):
        with pytest.raises(read.NothingAsked):
            call()
    assert web.request_count == 0


def test_search_channel_found_nothing():
    body = f'<html><body><div class="{tgweb.NO_MESSAGES_FOUND}">No results</div></body></html>'
    web = FakeWeb({preview_url("durov", query="zzznohitszzz"): {"status": 200, "body": body}})
    result = read.search_channel(web, "durov", "zzznohitszzz")

    assert result.requests == 1
    assert result.found_nothing is True
    assert result.exhausted is True


# --------------------------------------------------------------------------
# read_group_message -- one request, real hit and real miss bodies
# --------------------------------------------------------------------------
def test_read_group_message_hit(probe):
    """A11: the assertion here used to be `msg is not None`.

    This is the most expensive read path in the skill -- one HTTP GET per
    message id -- and it was pinned by checking that something came back. A
    parser returning a `Message` with every field empty passed. `SKILL.md` §5
    promises a permalink, a date verbatim from the page, the text and the
    author, so those are what this asserts, against the real saved page.
    """
    body = probe("C26-embed-hanoi-29327.html")
    web = FakeWeb({embed_url("hanoi_chats", 29327): {"status": 200, "body": body}})
    msg = read.read_group_message(web, "hanoi_chats", 29327)

    assert msg is not None
    assert msg.username == "hanoi_chats"
    assert msg.id == 29327
    assert msg.url == "https://t.me/hanoi_chats/29327"
    assert msg.date == "2026-08-22T17:58:18+00:00"
    assert msg.text == "Hi"
    assert msg.author_name == "Author Five"
    assert msg.is_service is False
    assert msg.found_by is None          # no query was behind this read


def test_read_group_message_miss(probe):
    body = probe("C16-embed-hanoi-10000.html")
    web = FakeWeb({embed_url("hanoi_chats", 10000): {"status": 200, "body": body}})
    msg = read.read_group_message(web, "hanoi_chats", 10000)
    assert msg is None


# --------------------------------------------------------------------------
# The measured group surface: 1.7 % density, a forced 124-id run of empties
# --------------------------------------------------------------------------
TRUE_HEAD = 29327          # what hanoi_chats actually served on 2026-08-24
MEASURED_DENSITY = 0.017   # 3 messages across 175 consecutive ids


def _sparse_group(seed: int, head: int = TRUE_HEAD,
                  density: float = MEASURED_DENSITY) -> set[int]:
    """A group occupied at the density a live group was measured to have."""
    rng = random.Random(seed)
    occupied = {i for i in range(1, head + 1) if rng.random() < density}
    # The measured window, forced exactly: 29327 and 29326 alive, 29202-29325
    # empty (124 in a row, and NOT the end of history), 29201 alive.
    occupied |= {head, head - 1, 29201}
    occupied -= set(range(29202, head - 1))
    return occupied


def _gapped_web(probe, occupied):
    """Serves the real hit page and the real "Post not found" page, per id.

    The hit body is `C16-embed-hanoi-1.html` with the single `data-post`
    attribute rewritten to the id being asked about; the miss body is
    `C26-embed-hanoi-29320.html` untouched. Nothing else about either page is
    synthetic -- and the two are byte-identical in every respect that could
    distinguish "deleted" from "past the end", which is the whole problem.
    """
    miss_body = probe("C26-embed-hanoi-29320.html")
    hit_body = probe("C16-embed-hanoi-1.html")

    class GappedGroupWeb:
        def __init__(self):
            self.request_count = 0
            self.asked: list[int] = []

        def embed(self, username, message_id, *, save_as=None):
            mid = int(message_id)
            self.request_count += 1
            self.asked.append(mid)
            if mid in occupied:
                body = hit_body.replace('data-post="hanoi_chats/1"',
                                        f'data-post="hanoi_chats/{mid}"')
            else:
                body = miss_body
            return tgweb.Response(
                url="fake", status=200, body=body, bytes=len(body.encode("utf-8"))
            )

    return GappedGroupWeb()


# --------------------------------------------------------------------------
# walk_group -- survives a gap, and is bounded in both directions
# --------------------------------------------------------------------------


class RetryingWeb:
    """A transport that needs two acts for some ids, as a retried 5xx does.

    `tgweb.fetch` counts every attempt, so one logical `embed()` can be two or
    three `request_count`. This is the smallest surface that reproduces the
    disagreement between a ceiling that counts calls and one that counts acts.
    """

    def __init__(self, inner, retry_every: int = 2):
        self.inner = inner
        self.retry_every = retry_every
        self.request_count = 0
        self.asked: list[int] = []

    def embed(self, username, message_id, *, save_as=None):
        self.asked.append(int(message_id))
        acts = 2 if len(self.asked) % self.retry_every == 0 else 1
        self.request_count += acts
        return self.inner.embed(username, message_id, save_as=save_as)


# --------------------------------------------------------------------------
# find_max_id -- the refusal, the hint, and the blind estimate
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# max_id_from_rss
# --------------------------------------------------------------------------


# ==========================================================================
# Every one of these was measured live on 2026-08-25 against t.me before it
# was written; the reproduction is named in the docstring so it does not
# have to be derived again.
# ==========================================================================
def _with_prev_cursor(body: str, before: int) -> str:
    """The real A01 page with its rel="prev" cursor rewritten.

    The only synthetic thing about it is the number in one href. No probe in
    the corpus is a multi-page SEARCH -- the live surface stops before it can
    produce one, which is the whole subject of `surface_truncated` below -- so
    a page that keeps publishing a moving cursor has to be made from a real one.
    """
    return body.replace('rel="prev" href="/s/durov?before=523"',
                        f'rel="prev" href="/s/durov?before={before}"')


# --------------------------------------------------------------------------
# An empty query is refused before the wire
# --------------------------------------------------------------------------
@pytest.mark.parametrize("query", ["", "   ", "\t\n", None])
def test_an_empty_query_is_refused_before_the_wire(query):
    """`tgweb.preview` drops an empty `q=` out of the URL, so
    `t.me/s/<name>` was fetched plain.

    Measured live 2026-08-25 on a large news channel:

        read.search_channel(web, "newschannel", "")    -> 20 messages
        read.search_channel(web, "newschannel", "   ") -> 20 messages

    `found: 20, found_nothing: false`, ids 98637-98643 -- the head of the
    channel -- every one of them stamped `found_by: ''`, provenance reading
    "this post was found by <nothing>". An agent whose term extraction folded
    away is handed the channel's front page as evidence about its subject.
    """
    web = FakeWeb({preview_url("newschannel"): {"status": 200, "body": "<html></html>"}})
    with pytest.raises(read.NothingAsked) as exc:
        read.search_channel(web, "newschannel", query, max_pages=1)
    assert "front page" in str(exc.value)
    assert web.calls == [], "the refusal must cost no request"


# --------------------------------------------------------------------------
# The ?q= cap is not an exhausted search
# --------------------------------------------------------------------------
def test_a_full_first_page_that_then_stops_is_a_cap_not_an_exhausted_search(probe):
    """The `?q=` surface serves ~20 hits and then stops, whatever really matches.

    Measured live 2026-08-25 on a news channel of 98 658 posts:

        tg.py search <channel> --query "<a word common in its posts>" --max-pages 5
        -> found 21, pages 2, exhausted True, stop_reason "no_more_pages"

    while `history --max-pages 3` over the SAME channel walked the 60 most
    recent posts and 32 of them contain the word. The surface served 21 for the
    whole channel; the last three pages alone hold 32. `exhausted` is documented
    as "the walk ended by itself rather than on a ceiling", which is read as
    *all the matches are in* -- and an agent writes "this channel has 21 posts
    on the subject" into a report as a fact.

    The same 20 + 2 ceiling came back for three more common words on the same
    channel and for two unrelated queries on two other channels, and
    `&before=` past the last hit returns nothing, so paging further is not a fix.
    What tells a cap from an ending is the FIRST page: full means the surface
    had more to give and stopped anyway.
    """
    a01 = probe("A01-s-durov.html")          # a full page: 20 hits, cursor 523
    c15 = probe("C15-s-durov-q-rare.html")   # 7 hits, terminal, no cursor
    web = FakeWeb({
        preview_url("durov", query="bitcoin"): {"status": 200, "body": a01},
        preview_url("durov", query="bitcoin", before=523): {"status": 200, "body": c15},
    })
    res = read.search_channel(web, "durov", "bitcoin", max_pages=5)

    assert res.pages == 2
    assert len(res.messages) == 27
    assert res.surface_truncated is True
    assert res.exhausted is False, "a cap is not a completed search"
    assert res.stop_reason == "surface_cap"
    assert res.stopped_early and "cap" in res.stopped_early
    assert "not be reported as a count" in res.stopped_early


def test_a_short_first_page_that_stops_really_is_the_end(probe):
    """The other side of the same discriminator, so the repair cannot be a
    blanket "never exhausted": the surface serves up to 20 hits a page, so a
    first page that came back short is all there was, and that walk IS
    exhausted."""
    c15 = probe("C15-s-durov-q-rare.html")
    web = FakeWeb({preview_url("durov", query="rare"): {"status": 200, "body": c15}})
    res = read.search_channel(web, "durov", "rare", max_pages=5)

    assert len(res.messages) == 7
    assert res.surface_truncated is False
    assert res.exhausted is True
    assert res.stop_reason == "no_more_pages"


def test_search_channel_stops_when_a_page_brings_no_new_post(probe):
    """The `not fresh` clause is the only defence against a surface that
    pages forever under a moving cursor, and nothing tested it: removing it left
    the suite green while the walk spent eight requests to be served the same
    twenty hits four times over, against a host whose rate limit has never been
    measured."""
    a01 = probe("A01-s-durov.html")
    web = FakeWeb({
        preview_url("durov", query="q"): {"status": 200, "body": a01},
        # Same twenty ids, a cursor that keeps moving. Without `not fresh` the
        # walk follows it to the page ceiling.
        preview_url("durov", query="q", before=523):
            {"status": 200, "body": _with_prev_cursor(a01, 500)},
        preview_url("durov", query="q", before=500):
            {"status": 200, "body": _with_prev_cursor(a01, 480)},
        preview_url("durov", query="q", before=480):
            {"status": 200, "body": _with_prev_cursor(a01, 460)},
        preview_url("durov", query="q", before=460):
            {"status": 200, "body": _with_prev_cursor(a01, 440)},
    })
    res = read.search_channel(web, "durov", "q", max_pages=8)

    assert res.pages == 2 and res.requests == 2
    assert len(res.messages) == 20
    assert res.stop_reason == "surface_cap"     # a full first page: see above
    assert res.exhausted is False


# --------------------------------------------------------------------------
# The two boundary cases nothing held
# --------------------------------------------------------------------------
def test_an_id_of_one_on_the_page_proves_the_first_post(probe):
    """`reached_first_post` is the ONE field that means "fully read", and the
    suite proved only the negatives. Changing `<= 1` to `<= 0` left the suite
    green while a completely read channel came back
    `reached_first_post: false, stop_reason: no_more_pages` -- which SKILL.md
    glosses as "the surface will serve nothing older", explicitly NOT as fully
    read -- and spent one extra request doing it."""
    # The real C15 page with its oldest post renumbered from 62 to 1 -- the one
    # thing no probe in the corpus holds, because reaching a channel's first
    # post takes a full crawl. Nothing else about the body is touched.
    first = probe("C15-s-durov-q-rare.html").replace(
        'data-post="durov/62"', 'data-post="durov/1"')
    assert min(m.id for m in tgparse.parse_preview(first, "durov").messages) == 1
    web = FakeWeb({
        preview_url("durov"): {"status": 200, "body": first},
        preview_url("durov", before=1): {"status": 200, "body": first},
    })
    res = read.walk_channel(web, "durov", max_pages=10)

    assert res.reached_first_post is True
    assert res.stop_reason == "first_post"
    assert res.exhausted is True
    assert res.requests == 1


def test_walk_channel_stops_on_the_page_whose_smallest_id_equals_until_id(probe):
    """The boundary: `any(m.id <= until_id)` against `<`. One request per
    `--since-last` walk spent re-reading a page the registry already covers."""
    a01 = probe("A01-s-durov.html")
    smallest = min(m.id for m in tgparse.parse_preview(a01, "durov").messages)
    web = FakeWeb({
        preview_url("durov"): {"status": 200, "body": a01},
        preview_url("durov", before=523): {"status": 200, "body": a01},
    })
    res = read.walk_channel(web, "durov", until_id=smallest, max_pages=10)

    assert res.pages == 1, "the page carrying the cursor id is where it stops"
    assert res.requests == 1
    assert res.reached_until_id is True
    assert all(m.id > smallest for m in res.messages)


# --------------------------------------------------------------------------
# An album is one block carrying several ids, and every id is accounted for
# --------------------------------------------------------------------------
class _Album:
    """A message block standing for several ids, as `tgparse` will hand it over.

    Written against the shape a page is required to hand over rather than
    against `tgparse` itself, so the id accounting is testable whether or not
    the parse half has landed: `Message.ids` sorted and always containing `Message.id`,
    `Page.ids_seen` the count of distinct ids on the page.
    """

    def __init__(self, first: int, span: int = 1, username: str = "nexta_tv",
                 anchor: int | None = None):
        self.ids = list(range(first, first + span))
        # The `data-post` id the block is filed under. It is NOT always the
        # smallest of them: on the live nexta_tv page two of the album's ids
        # were in the markup and not as `data-post` at all.
        self.id = self.ids[0] if anchor is None else anchor
        assert self.id in self.ids
        self.username = username
        self.url = f"https://t.me/{username}/{self.id}"
        self.text = ""
        self.found_by = None


class _AlbumPage:
    def __init__(self, messages, before=None):
        self.messages = list(messages)
        self.before = before
        self.after = None
        self.blocks_seen = len(self.messages)
        self.blocks_unparsed = 0
        self.understood_nothing = False
        self.ids_seen = len({i for m in self.messages for i in m.ids})


def _album_pages(monkeypatch, pages):
    """Serve a prepared `_AlbumPage` per request, in order."""
    served = iter(pages)
    monkeypatch.setattr(read.tgparse, "parse_preview",
                        lambda *a, **k: next(served))


def test_a_page_of_albums_is_counted_in_ids_not_in_blocks(monkeypatch, probe):
    """Verified live on `t.me/s/nexta_tv`: the page carried
    ids 27033-27052, only 18 `data-post` attributes, and 27043/27044 were in the
    markup but not as `data-post`.

    `len(page.messages)` therefore says 18 where the page really accounted for
    20, and page-fullness decides whether a `?q=` walk calls its stop a cap or
    an exhausted search. Eighteen of twenty is not a full page; twenty is.
    """
    a01 = probe("A01-s-durov.html")
    page = _AlbumPage([_Album(27033, 5), _Album(27038, 5), _Album(27043, 10)])
    assert len(page.messages) == 3 and page.ids_seen == 20

    _album_pages(monkeypatch, [page])
    web = FakeWeb({preview_url("nexta_tv", query="q"): {"status": 200, "body": a01}})
    res = read.search_channel(web, "nexta_tv", "q", max_pages=3)

    assert res.ids_seen == 20, "the ids the page accounted for, not its blocks"
    assert res.surface_truncated is True, "20 ids IS a full page"
    assert res.exhausted is False


def test_an_album_straddling_the_cursor_is_re_read_not_skipped(monkeypatch, probe):
    """The tie-break when a block straddles the cursor: "take the one that re-reads
    rather than the one that skips -- a duplicate costs a request, a skip loses
    a post silently". A block whose ids run 528-533 across an `until_id` of 530
    carries three ids the previous run never saw, and filtering it out by its
    anchor id alone throws all six away."""
    a01 = probe("A01-s-durov.html")
    _album_pages(monkeypatch, [_AlbumPage([_Album(540, 2), _Album(528, 6)])])
    web = FakeWeb({preview_url("durov"): {"status": 200, "body": a01}})
    res = read.walk_channel(web, "durov", until_id=530, max_pages=3)

    kept = sorted(m.id for m in res.messages)
    assert kept == [528, 540], "the straddling album is kept, ids and all"
    assert res.reached_until_id is True
    assert res.stop_reason == "until_id"


def test_an_album_reaching_id_one_proves_the_first_post(monkeypatch, probe):
    """The same field again, decided by `min(ids)` and not by `min(m.id)`: an
    album whose anchor is 4 and whose ids run down to 1 has reached the first
    post, and a walk reading only the anchor pays another request to be told
    the surface has nothing older."""
    a01 = probe("A01-s-durov.html")
    _album_pages(monkeypatch, [_AlbumPage([_Album(1, 4, anchor=4)], before=None)])
    web = FakeWeb({preview_url("durov"): {"status": 200, "body": a01}})
    res = read.walk_channel(web, "durov", max_pages=5)

    assert res.reached_first_post is True
    assert res.stop_reason == "first_post"
    assert res.requests == 1


# --------------------------------------------------------------------------
# A hint is not evidence until something answers
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Zero means do nothing, and never means unlimited
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Damage from a config file or a hostile argument is named, not raised
# --------------------------------------------------------------------------
# `None` is deliberately absent: for `walk_channel` it is the documented way to
# ask for the configured budget, not damage.


def test_ids_seen_is_derived_when_the_page_does_not_carry_it(probe):
    """The id accounting lands in two halves, so this one must work either
    side of the parse growing `Page.ids_seen`: with the field absent, the count is
    derived from the ids the blocks carry, and `len(messages)` is never the
    answer. A page of 3 album blocks accounting for 20 ids is a FULL page, and
    page-fullness is what tells a `?q=` cap from an exhausted search.
    """
    page = _AlbumPage([_Album(27033, 5), _Album(27038, 5), _Album(27043, 10)])
    del page.ids_seen
    assert not hasattr(page, "ids_seen")
    assert read._page_ids_seen(page) == 20
    assert len(page.messages) == 3

    # And a plain page of single posts is unchanged by any of it.
    plain = tgparse.parse_preview(probe("A01-s-durov.html"), "durov")
    assert read._page_ids_seen(plain) == len(plain.messages) == 20


# --------------------------------------------------------------------------
# An id whose GET raised was spent, and is charged for
# --------------------------------------------------------------------------
class _StoppedWeb:
    """Answers from `inner` until `stop_at` acts, then stops the run.

    Two flavours, because the two are charged oppositely and the transport is what
    tells them apart. `reached_wire=True` mirrors a 429: `tgweb.fetch` builds the
    Response, increments `request_count`, and only then does `stop_signal` raise
    -- the act had already left the machine. `reached_wire=False` mirrors a
    refusal taken before the request was built (`aborted_reason` already set),
    where `request_count` never moves.
    """

    def __init__(self, inner, *, stop_at: int, reached_wire: bool):
        self.inner = inner
        self.stop_at = stop_at
        self.reached_wire = reached_wire

    @property
    def request_count(self) -> int:
        return self.inner.request_count

    def embed(self, username, message_id, *, save_as=None):
        if self.inner.request_count >= self.stop_at:
            if self.reached_wire:
                self.inner.request_count += 1
            raise tgweb.RunAborted("429 from t.me: slow down")
        return self.inner.embed(username, message_id, save_as=save_as)


# --------------------------------------------------------------------------
# The two shared helpers, pinned against the surfaces that still call them
# --------------------------------------------------------------------------
# `None` is deliberately absent: for `walk_channel` it is the documented way to
# ask for the configured budget, not damage.
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"),
                                 "twenty", [3]])
def test_a_ceiling_that_is_not_a_finite_number_is_refused_by_name(bad):
    """`json.loads` accepts NaN and Infinity and both pass `isinstance(x, float)`.

    NaN makes every comparison false, so a walk with `max_pages=nan` runs with no
    ceiling while reporting one; `int(inf)` raises OverflowError, which was in no
    except clause and left a `cmd_*` as a traceback and exit 1 -- the code
    reserved for a crash.

    The group half of this test went out with `walk_group` on 2026-08-25, and
    `_want_count` is still the gate on both channel ceilings and on the message
    id `group --id` asks for. Restored against those.
    """
    web = FakeWeb({})
    with pytest.raises(read.NothingAsked):
        read.walk_channel(web, "durov", max_pages=bad)
    with pytest.raises(read.NothingAsked):
        read.search_channel(web, "durov", "arenda", max_pages=bad)
    with pytest.raises(read.NothingAsked):
        read.read_group_message(web, "hanoi_chats", bad)
    assert web.calls == [], "a refusal must not spend a request"


def test_a_damaged_budget_in_config_falls_back_instead_of_crashing(monkeypatch):
    """The same damage arriving through `config.Budgets` rather than an argument."""
    class _Poisoned:
        max_pages_per_channel = float("inf")

    monkeypatch.setattr("config.Budgets", _Poisoned)
    assert read._budget("max_pages_per_channel", 25) == 25

    class _Boolean:
        # `true` in a JSON config. `isinstance(True, int)` is True and
        # `int(True)` is 1, so without the shared check this is a page ceiling
        # of ONE -- a walk that stops after a page and reports a ceiling it was
        # never given.
        max_pages_per_channel = True

    monkeypatch.setattr("config.Budgets", _Boolean)
    assert read._budget("max_pages_per_channel", 25) == 25
