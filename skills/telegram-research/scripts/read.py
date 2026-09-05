"""Stage 4 -- reading, by a route the source's type chooses.

The rule the whole stage rests on: **channels are free, groups are free but
expensive per message.**

* A channel closes completely with no account. `?q=` searches its entire history
  in one request; `?before=` walks back to its first post twenty messages at a
  time. The ban surface is zero because there is no account involved, and the
  account is never touched for a channel -- that is a rule, not an optimisation.
* A group has no `/s/` page at all and no free search surface. `?embed=1`
  serves ONE message for ONE known id, which is right for checking a claim and
  for nothing else: the id has to be known already, and about 1 % of a group's
  id range answers, so hunting for the ids by trying them is not a search. It
  was one, it cost 200 requests for 2 messages and 0 hits on the word asked
  about, and it is gone. **Searching a group goes through the account**
  (`messages.search`, `tg.py search`): 1 call, 44 hits, no resolve.

Trying `/s/` on a group is a defect of this skill, not a network failure: the
type is in the registry and it decides the route.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import tgparse
from tgweb import post_missing, preview_available, search_found_nothing


class WrongRoute(RuntimeError):
    """A group was read as a channel, or the reverse. Always our bug."""


class NothingAsked(ValueError):
    """The call cannot return anything, so it is refused instead of answered.

    `--max-pages 0` and a group walk whose floor is already at or above its
    start id both spend zero requests and used to come back `found: 0,
    ok: true`. That output is indistinguishable from "the surface really is
    empty", and an agent writes it into a report as a fact -- which is the one
    thing this skill exists not to do. `cmd_group` already refuses `--count 0`
    in exactly those words; every other way of asking a question with no ids in
    it is refused here, in the one place all of them pass through.

    The caller that DID establish something -- a catch-up that scanned above the
    cursor and found nothing -- catches this and says so with its own evidence.
    `read` has none of that evidence and must not invent it.
    """


# Fallbacks for the two ceilings that belong in `config.Budgets`. They are read
# from there at call time rather than imported as constants, so that changing a
# budget changes behaviour without a code edit -- and so that a config module
# that has moved on cannot stop `read` from importing.
FALLBACK_CHANNEL_PAGE_CEILING = 25     # config.Budgets.max_pages_per_channel


def _budget(name: str, fallback: int) -> int:
    try:
        import config
        value = getattr(config.Budgets(), name)
        # A budget is a number read out of a config override, so NaN and
        # Infinity reach it. `int(inf)` raises OverflowError, which was in no
        # except clause here and left `walk_channel` as a traceback; `int(nan)`
        # raises ValueError, which was caught and quietly became the fallback.
        # Both are damage, and the shared check is what names them.
        return int(config.want_finite_number({name: value}, name))
    except (ImportError, AttributeError, TypeError, ValueError, OverflowError):
        return fallback


def _want_count(value, name: str) -> int:
    """A ceiling from a caller or the command line, as a whole number.

    A public entry point of this module raises the skill's own exception
    types and never a bare `ValueError`, `OverflowError` or `TypeError` out of
    `int()`. A ceiling that is not a number is a call that cannot say what it
    was asked for, which is what `NothingAsked` means -- and being a ValueError
    it lands on the CLI's usage exit rather than on the code reserved for a
    crash.

    The finite check is `config.want_finite_number` and is not copied here.
    """
    try:
        import config
    except ImportError:                 # pragma: no cover -- config is a sibling
        pass
    else:
        try:
            value = config.want_finite_number({name: value}, name)
        except ValueError as exc:
            raise NothingAsked(
                f"{name}={value!r}: {exc}. A ceiling that is not a finite number "
                "makes every comparison against it false, so the walk would run "
                "with no ceiling at all while reporting that it had one"
            ) from exc
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise NothingAsked(
            f"{name}={value!r} is not a whole number of things to do, so this "
            "call cannot report what it was asked for"
        ) from exc


# How many hits a full `?q=` page carries. `tgparse` measured it on every
# non-terminal probe page in the corpus; the fallback is the same number.
PAGE_SIZE = getattr(tgparse, "PAGE_SIZE", 20)


def _ids_of(msg) -> list[int]:
    """Every id this message block accounts for.

    An album is ONE block carrying several ids: `t.me/s/nexta_tv` served ids
    27033-27052 under 18 `data-post` attributes, with 27043 and 27044 present in
    the markup and not as `data-post`. `Message.ids` is where they live; a lone
    post is `[id]`. Written to work either side of that field existing, because
    counting a page by `len(messages)` is wrong the moment one album is on it.
    """
    ids = getattr(msg, "ids", None) or ()
    out = {int(i) for i in ids}
    out.add(int(msg.id))
    return sorted(out)


def _page_ids_seen(page) -> int:
    """`Page.ids_seen` -- distinct ids across the page's messages."""
    seen = getattr(page, "ids_seen", None)
    if isinstance(seen, int) and not isinstance(seen, bool) and seen > 0:
        return seen
    return len({i for m in page.messages for i in _ids_of(m)})


def _page_is_full(page) -> bool:
    """`PreviewPage.is_full` -- ids OR blocks, whichever is the larger count.

    Read off the page rather than recomputed, because "was this page full" has
    to be answered the same way everywhere: `tgparse._cursors` decides whether
    to fall back to a cursor with it, and this module decides with it whether a
    `?q=` search that stopped had been capped or had run out of hits. Counting
    only the ids seen -- which is what this did -- reads a page of 20 blocks of
    which 3 did not parse as a SHORT page, i.e. as the end of the matches, i.e.
    as `exhausted: true` on a search the surface had truncated.
    """
    full = getattr(page, "is_full", None)
    if isinstance(full, bool):
        return full
    blocks = int(getattr(page, "blocks_seen", 0) or 0)
    return max(_page_ids_seen(page), blocks) >= PAGE_SIZE


def _page_min_id(page) -> int | None:
    """The smallest id ON the page -- `min(ids)`, never `min(m.id)`."""
    ids = [i for m in page.messages for i in _ids_of(m)]
    return min(ids) if ids else None


@dataclass
class ReadResult:
    """What a walk brought back, and -- separately -- why it stopped.

    One boolean used to answer three different questions. `exhausted` was set
    when `until_id` was reached, when the cursor stopped moving, and when an id
    at or below 1 turned up, and `tg.py` printed it as `reached_first_post`.
    Only the last of those means that. The four fields below say which of them
    happened, and `exhausted` is kept with the one meaning it can carry
    honestly: the walk ended by itself rather than on a ceiling.
    """

    username: str
    messages: list = field(default_factory=list)
    pages: int = 0
    requests: int = 0
    exhausted: bool = False          # the walk ended by itself, not on a ceiling
    stopped_early: str | None = None  # the budget or signal that cut it short
    found_nothing: bool = False
    reached_first_post: bool = False  # PROVEN: an id <= 1 was on the last page
    reached_until_id: bool = False    # caught up with what a previous run stored
    no_more_pages: bool = False       # the surface published no further cursor
    stop_reason: str | None = None    # short code naming which of these ended it
    understood_nothing: bool = False  # a page carried message blocks and parsed none
    blocks_unparsed: int = 0          # how many blocks went unread across the walk
    # The `?q=` surface stopped serving while its first page was FULL. That is
    # its own ceiling, not the end of the matches, and `exhausted` stays False.
    surface_truncated: bool = False
    # Distinct message ids the pages accounted for. Not `len(messages)`:
    # an album is one message block carrying several ids, so the two differ by
    # however many albums the walk crossed.
    ids_seen: int = 0

    def _end(self, reason: str) -> None:
        """Mark a natural end -- the walk ran out of material, not of budget."""
        self.exhausted = True
        self.stop_reason = reason

    def _search_end(self, reason: str, *, first_page_full: bool) -> None:
        """End a `?q=` search, and refuse to call a CAP an exhausted search.

        `exhausted` is documented as "the walk ended by itself rather than on a
        ceiling", which every caller reads as *all the matches are in*. On this
        surface that is not something a stop can establish. Measured live
        2026-08-25 on a news channel of 98 658 posts: a query on a word common
        in its material served 20 hits on page 1, 1 more on the paging hop and
        then no cursor at all -- 21 for the whole channel -- while a three-page
        `history` walk of the 60 most recent posts found that word in 32 of
        them. `?q=` served 21 and stopped; the last three pages alone hold 32.
        The same 20 + 2 ceiling came back for three more common words on the
        same channel and for two unrelated queries on two other channels, and
        `&before=` past the last hit returns nothing, so following the cursor
        further is not a fix either.

        What separates the two cases is the FIRST page. The surface serves up to
        `PAGE_SIZE` hits a page; a first page that came back short is all there
        was, and a first page that came back full and then stopped is this
        surface's own ceiling. So a full first page ends the search as
        `surface_cap`, `exhausted` stays False, and the sentence goes into
        `stopped_early` -- which is the one field `tg.py cmd_search` already
        prints.
        """
        self.no_more_pages = True
        if not first_page_full:
            self._end(reason)
            return
        self.surface_truncated = True
        self.stop_reason = "surface_cap"
        self.stopped_early = (
            f"the ?q= surface filled its first page ({PAGE_SIZE} hits) and then "
            f"stopped serving, at {len(self.messages)} hits in total. That is "
            "this surface's own cap, NOT the end of the matches: measured "
            "2026-08-25, a news channel of 98 658 posts answered 21 for a "
            "query whose word appears in 32 of its last 60 posts. This is a "
            "partial result and must not be reported as a count of what the "
            "channel said — walk the history with `history` if the number "
            "matters"
        )

    def _unreadable(self, page) -> bool:
        """Did this page carry message blocks and yield none of them?

        A read that understood nothing is not a read that found nothing, and
        until `tgparse` grew `blocks_seen` the two were the same output: a page
        whose blocks stopped yielding `data-post` parsed as zero messages with a
        live cursor, so a front-end change looked exactly like a quiet channel
        and the walk paged on through pages it could not read, spending the
        ceiling and reporting silence.

        So it ends the walk, and under a stop reason of its own. `exhausted`
        stays False: nothing about this walk ran out of material, and nothing
        here is evidence of absence. What was already read is kept -- the pages
        before the change parsed fine.
        """
        self.blocks_unparsed += int(getattr(page, "blocks_unparsed", 0) or 0)
        if not getattr(page, "understood_nothing", False):
            return False
        self.understood_nothing = True
        self.stop_reason = "understood_nothing"
        # `understood_nothing` has two shapes and this sentence described one of
        # them for both. In the second, every block on the page DID parse and
        # none of them carries a word of text -- the text selector has moved --
        # and telling the reader "not one of them parsed" sends whoever repairs
        # it looking at `data-post`, which is the half that still works.
        blocks = getattr(page, "blocks_seen", 0)
        if not getattr(page, "messages", None):
            what = (f"{blocks} message blocks on this page and not one of them "
                    "parsed — the markup this skill reads has changed")
        else:
            what = (f"{len(page.messages)} message blocks on this page all "
                    "parsed and not one of them carries any text — the text "
                    "selector has moved")
        self.stopped_early = (
            f"{what}. That is a front-end change to report, NOT an empty page: "
            "nothing here is evidence that the channel said nothing"
        )
        return True


def _attach_partial(exc: BaseException, result: ReadResult) -> None:
    """Hang the harvest so far on the exception that interrupted the walk.

    A stop signal is correct; losing what was already fetched on top of it is
    not. `tgweb.RunAborted` propagates unchanged -- the caller's `except` clause
    keeps working exactly as it did -- but it now carries `exc.partial`, so the
    CLI can write the posts it already paid for and update the cursor before it
    reports the stop.
    """
    try:
        exc.partial = result
    except AttributeError:      # an exception type that forbids attributes
        pass


# --------------------------------------------------------------------------
# Channels
# --------------------------------------------------------------------------
def search_channel(web, username: str, query: str, *, max_pages: int = 5,
                   save_prefix: str | None = None) -> ReadResult:
    """`t.me/s/<name>?q=<query>` -- server-side full-text search of the history.

    One request answers a question that would otherwise cost a full crawl: a
    query against @durov returned seven posts spanning ids 62 to 440 in a single
    62 KB page. Search is cheaper than paging by about two orders of magnitude,
    which is why a full crawl has to be a decision somebody makes rather than
    the default.

    Paging a search past twenty hits is done with the same `before` cursor as an
    ordinary walk. That the parameter survives pagination is how the only other
    implementation of this surface does it too; our own probes never returned
    enough hits to trigger a second page, so a run that does page here is worth
    reading closely the first time.

    Every message this returns carries `found_by=query`: which query found a post
    is the provenance the whole vocabulary loop is built on, and it is set here,
    on the `?q=` route, and on no other route.

    **An empty or whitespace-only query is refused here, before the wire.**
    `tgweb.preview` does `if query: params["q"] = query`, so an empty query was
    dropped out of the URL and `t.me/s/<name>` fetched plain: the channel's 20
    most recent posts came back as `found: 20, found_nothing: false` with every
    `found_by` set to the empty string -- provenance reading "this post was
    found by <nothing>". An agent whose term extraction folded to nothing was
    handed the channel's front page as evidence about its subject. Measured
    2026-08-25 on a large news channel for both `""` and `"   "`.
    """
    max_pages = _want_count(max_pages, "max_pages")
    if max_pages < 1:
        raise NothingAsked(
            f"--max-pages {max_pages} asks for no pages, so this search cannot "
            "return a hit it did have. A `found: 0` from a query nobody ran is a "
            "silence an agent would write into a report as a fact."
        )
    if query is None or not str(query).strip():
        raise NothingAsked(
            f"{username}: {query!r} is not a query. An empty one is dropped out "
            "of the URL by the transport, so what comes back is the channel's "
            "front page — its 20 newest posts, labelled as hits for a search "
            "nobody ran and stamped `found_by: ''`. Say what to look for, or use "
            "`history` if the front page is what is wanted."
        )
    result = ReadResult(username=username)
    before = None
    seen: set[int] = set()
    first_page_full = False
    try:
        for page_no in range(max_pages):
            label = f"{save_prefix or username}-q-{_slug(query)}-{page_no}.html" if save_prefix else None
            resp = web.preview(username, query=query, before=before, save_as=label)
            result.requests += 1
            if resp.redirected:
                raise WrongRoute(
                    f"{username}: /s/ answered {resp.status} -> {resp.location}. "
                    "This name is a group or does not exist. `search` routes a "
                    "group to the account instead of this page, but only when the "
                    "registry types it one: run `verify <name> --write` first."
                )
            if search_found_nothing(resp.body):
                result.found_nothing = page_no == 0
                if page_no == 0:
                    result._end("found_nothing")
                    result.no_more_pages = True
                else:
                    result._search_end("no_more_pages",
                                       first_page_full=first_page_full)
                break
            # A `/s/` page with no message block on it at all, and without the
            # zero-hit marker the branch above reads. `walk_channel` has always
            # stopped here; this route did not, and ran on to the cursor test,
            # where a page carrying no cursor either ended the search as
            # `no_more_pages` -- `found: 0, exhausted: true, found_nothing:
            # false`, which is a false zero wearing the shape of a real ending.
            # Nothing about such a page says the query matched nothing: it is a
            # front end that changed, a wall, or a surface having a bad minute.
            if not preview_available(resp):
                result.stopped_early = (
                    "the ?q= page carried no message blocks and no zero-hit "
                    "marker either — nothing here says the query matched "
                    "nothing, so this search is unfinished, not empty"
                )
                result.stop_reason = "no_messages"
                break
            page = tgparse.parse_preview(
                resp.body, username, found_by=query,
                source_file=resp.headers.get("x-saved-as"),
            )
            if result._unreadable(page):
                break
            # A block whose ids are not ALL known is fresh. "Take the one
            # that re-reads rather than the one that skips" -- an album served
            # again under a different anchor id costs a duplicate, dropping it
            # loses the ids it carried that nothing else did.
            fresh = [m for m in page.messages if not seen.issuperset(_ids_of(m))]
            for m in fresh:
                seen.update(_ids_of(m))
            result.messages.extend(fresh)
            result.pages += 1
            result.ids_seen = len(seen)
            if page_no == 0:
                first_page_full = _page_is_full(page)
            # The last page of hits ends the search HERE, without paying a
            # request to be told the next one is empty. That rests entirely on
            # `tgparse._cursors` returning `before=None` for a page that
            # publishes no cursor of its own: while it fell back to the smallest
            # id on every page, this branch could never fire and every query cost
            # pages + 1 per channel. If a fallback on a short page ever comes
            # back, `test_search_channel_real_rare_query` goes red here.
            if not fresh or page.before is None or page.before == before:
                result._search_end("no_more_pages", first_page_full=first_page_full)
                break
            before = page.before
        else:
            result.stopped_early = f"page ceiling of {max_pages} reached"
            result.stop_reason = "page_ceiling"
    except Exception as exc:                    # noqa: BLE001 -- re-raised below
        # Not a catch: the exception carries on untouched. It only leaves with
        # the pages already paid for attached to it.
        result.messages.sort(key=lambda m: m.id)
        result.stop_reason = "aborted"
        _attach_partial(exc, result)
        raise
    result.messages.sort(key=lambda m: m.id)
    return result


def walk_channel(web, username: str, *, before: int | None = None,
                 max_pages: int | None = None, until_id: int | None = None,
                 save_prefix: str | None = None) -> ReadResult:
    """Walk a channel backwards through `?before=`, twenty messages a page.

    The cursor comes from the page itself, and where the page does not publish
    one it falls back to the smallest id actually seen -- never to arithmetic.
    Ids have gaps, so stepping back by twenty walks straight over live posts
    without ever asking for them.

    `until_id` is how a second run avoids re-reading what the first one already
    has: the registry keeps the last id read, and the walk stops when it gets
    back to it. Never fetching twice what has been fetched once is a rule of this
    skill, and it is the only defence there is against an unmeasured IP limit.

    Four different things end this walk and they are not the same thing, so
    they are reported separately -- `reached_until_id`, `no_more_pages`,
    `reached_first_post` and `understood_nothing`. Only the third is proof that
    the channel is fully read, and it is set only when an id at or below 1 was
    actually on a page. The fourth is not an ending at all in the sense the
    others are: it means the page carried messages this skill could no longer
    read, and it is a front-end change to report rather than anything about the
    channel.
    """
    if max_pages is None:
        max_pages = _budget("max_pages_per_channel", FALLBACK_CHANNEL_PAGE_CEILING)
    max_pages = _want_count(max_pages, "max_pages")
    if max_pages < 1:
        raise NothingAsked(
            f"--max-pages {max_pages} asks for no pages, so this walk cannot "
            "return a post the channel did have. A `found: 0` from a walk nobody "
            "ran is a silence an agent would write into a report as a fact."
        )
    result = ReadResult(username=username)
    seen: set[int] = set()
    cursor = before
    try:
        for page_no in range(max_pages):
            label = f"{save_prefix or username}-before-{cursor or 'head'}.html" if save_prefix else None
            resp = web.preview(username, before=cursor, save_as=label)
            result.requests += 1
            if resp.redirected:
                raise WrongRoute(
                    f"{username}: /s/ answered {resp.status} -> {resp.location}. "
                    "Groups have no preview page. `history` routes a group to "
                    "the account instead of this page, but only when the "
                    "registry types it one: run `verify <name> --write` first. "
                    "`group --id` reads one known id without an account."
                )
            if not preview_available(resp):
                result.stopped_early = "the preview page carried no messages"
                result.stop_reason = "no_messages"
                break
            page = tgparse.parse_preview(
                resp.body, username, source_file=resp.headers.get("x-saved-as")
            )
            if result._unreadable(page):
                break
            # As in `search_channel`: freshness and the two boundary tests
            # below run on every id a block accounts for, never on `m.id` alone.
            fresh = [m for m in page.messages if not seen.issuperset(_ids_of(m))]
            for m in fresh:
                seen.update(_ids_of(m))
            if until_id is not None:
                # An album straddling the cursor is KEPT: it carries at least
                # one id the previous run never saw, and re-reading the rest of
                # it costs nothing more than the request already paid for.
                fresh = [m for m in fresh if max(_ids_of(m)) > until_id]
            result.messages.extend(fresh)
            result.pages += 1
            result.ids_seen = len(seen)

            lowest = _page_min_id(page)
            if until_id is not None and lowest is not None and lowest <= until_id:
                result.reached_until_id = True
                result._end("until_id")
                break
            # A page can carry a `?before=` link and no parseable message at all
            # -- twenty service messages carry no `data-post` between them, and
            # tgparse reads the cursor straight out of the body. `min()` over an
            # empty page used to raise here and lose the whole walk.
            if lowest is not None and lowest <= 1:
                result.reached_first_post = True
                result.no_more_pages = True
                result._end("first_post")
                break
            if page.before is None or page.before == cursor:
                # No further cursor. This surface will serve nothing older --
                # which is NOT the same as "this is the channel's first post":
                # a channel whose earliest posts were deleted ends here too.
                result.no_more_pages = True
                result._end("no_more_pages")
                break
            cursor = page.before
        else:
            result.stopped_early = f"page ceiling of {max_pages} reached"
            result.stop_reason = "page_ceiling"
    except Exception as exc:                    # noqa: BLE001 -- re-raised below
        result.messages.sort(key=lambda m: m.id)
        result.stop_reason = "aborted"
        _attach_partial(exc, result)
        raise
    result.messages.sort(key=lambda m: m.id)
    return result


# --------------------------------------------------------------------------
# Groups
# --------------------------------------------------------------------------
def _fetch_group_message(web, username: str, message_id: int, *,
                         save_prefix: str | None = None):
    """One group message. Returns `(message | None, verdict)`.

    Verdict is `"hit"`, `"missing"`, `"unreadable"` or `"wrong_post"`.

    `"unreadable"` is `tgweb.embed_unreadable`'s third answer, and it was being
    given as `"missing"`. "This id carries no message" and "this page is not one
    this skill can read" are different facts: the first is data -- a gap, a
    deletion -- and the second is a front-end change, a join wall or an
    interstitial. Reported as the first, a stretch of unreadable pages is a
    stretch of proven-empty ids, and a walk ends on it saying the group's
    history stops there while the group is still talking.

    `"wrong_post"` is why this function exists at all: `parse_embed` takes the
    id and the username from the page's own
    `data-post`, and `tgweb.embed` fetches with `follow=True`, so a redirect is
    chased. An id that redirects to a linked discussion group or to a renamed
    peer used to be appended as if it were the id that was asked for -- counted
    against `--count`, written to `posts.jsonl` under another peer's name, and
    its id offered to `--write` as *this* group's cursor.

    `tgparse.parse_embed` is what notices: it sets `requested_id` when the post
    it parsed is not the post that was asked for, and leaves the decision here,
    because a record that is internally consistent is not the same as a record
    of the right id. The decision is that it is a miss. The id was asked and
    nothing came back for it, which is what a miss means; the `"wrong_post"`
    verdict is returned rather than swallowed, and `cmd_group` collects those
    ids into `mismatched_ids` so the caller can tell them from `missing_ids`.

    The two comparisons below repeat the parser's, deliberately: this is the
    walk's own guarantee that it never books a hit for an id it did not receive,
    and it must not depend on a flag another module remembers to set. The peer
    match is case-insensitive, so a landing page answering `BirdingChats` for
    `birding_chats` is the same peer, not a mismatch.
    """
    message_id = _want_count(message_id, "message_id")   # a named refusal
    label = f"{save_prefix or username}-{message_id}.html" if save_prefix else None
    resp = web.embed(username, message_id, save_as=label)
    if post_missing(resp.body):
        return None, "missing"
    msg = tgparse.parse_embed(
        resp.body, username, message_id,
        source_file=resp.headers.get("x-saved-as"),
    )
    if msg is None:
        # The `post_missing` test is repeated deliberately, exactly as the two
        # comparisons below repeat the parser's: a miss is only ever booked
        # against a page that PROVES the id is empty, and never against one that
        # merely failed to parse.
        return None, ("missing" if post_missing(resp.body) else "unreadable")
    same_peer = (msg.username or "").lstrip("@").casefold() == username.lstrip("@").casefold()
    if getattr(msg, "requested_id", None) is not None or msg.id != int(message_id) \
            or not same_peer:
        return None, "wrong_post"
    return msg, "hit"


def _slug(text: str, limit: int = 24) -> str:
    return re.sub(r"[^\w]+", "-", (text or "").lower()).strip("-")[:limit] or "q"
