"""Stage 2 -- finding out which channels and groups talk about a subject.

Not one resolve happens here, and not one account call. Everything on this stage
is free and anonymous, which is what makes it safe to be wide.

The stage is finished only when **at least two channels of a different nature**
have both produced candidates. One channel agreeing with itself is not
corroboration, and each of the four has a different blind spot:

* `lyzem` searches message text across many channels, and its index is thin --
  51 hits for an everyday word. It sees a small share and does not
  say which share.
* Ordinary web search sees what Google indexed, which for a group is only the
  landing page -- and a group's landing page carries no messages at all.
* Catalogues see what someone chose to list.
* Links inside already-found posts see what the corpus itself points at, which
  is the only channel with no editor in the middle.

**No third-party service is ever proof of absence.** "Lyzem found nothing" means
"its index holds nothing", and that is how the report has to say it.
"""

from __future__ import annotations

import re
import sys
import urllib.parse
from dataclasses import dataclass, field

import tgdom
import tgparse
from registry import AdmissionRules, Registry, Source, TopicClassifier, judge, today_local

# Every form a public peer's link is written in, not just the canonical one.
#
# `telegram.me` is Telegram's own alternative domain and is what older catalogue
# pages, forum posts and search results carry; `telegram.dog` is the third
# official alias; `tg://resolve?domain=<name>` is what a great many web pages
# emit for the deep link. Matching only the literal host `t.me` dropped all
# three with no counter and no note -- and the web and catalogue channels of
# stage 2 are exactly the ones that carry them, so it was a whole source
# silently never entering a run. Measured on a catalogue-shaped blob naming nine
# peers: three of them missed.
#
# Both patterns are anchored to a host boundary, which they were not.
# The left edge is the lookbehind on the host: without it the host had no
# beginning, so any domain ENDING in one of these -- `chatt.me/newsroom`,
# `bestt.me/channel`, `first-t.me/...` -- handed back the path as a channel
# name. The lookahead on `AT_RE` is the right edge: `@company.com` is a mail
# domain and came back as the channel «company». Each phantom costs one
# `t.me/<name>` GET to disprove and lands in `ranked()` with a hit, so it also
# pushes real candidates down the list the agent is told to verify in order. A
# handle at the end of an English sentence keeps working: a dot followed by a
# space is not a TLD.
USERNAME_RE = re.compile(
    r"(?<![\w-])(?:(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/(?:s/)?"
    r"|tg://resolve\?domain=)([A-Za-z0-9_]{4,32})\b",
    re.I,
)
AT_RE = re.compile(r"(?<![\w/])@([A-Za-z0-9_]{4,32})\b(?!\.[A-Za-z]{2,})")

# t.me paths that are routing furniture rather than a peer, plus the handful of
# self-promotion accounts the discovery services inject into their own pages.
# Every discovery channel emits some of these.
#
# `durov` is deliberately NOT here. It is a real channel and the largest one on
# the platform; it sits in this list in half the example code on the internet
# purely because it is the canonical demo name, and excluding a genuine source
# because it is famous is a bug, not a filter.
NOT_A_SOURCE = {
    "telegram", "share", "joinchat", "addstickers", "proxy", "socks",
    "iv", "s", "c", "setlanguage", "addtheme", "addemoji", "bot",
    "contact", "login", "auth", "lyzemcom", "telemetr",
}


def _warn(message: str) -> None:
    """Say on stderr, in one line, that a third-party surface has moved.

    Same job as `tgparse._warn` and reserved for the same class of event: not
    "this query found little" but "the page we asked is not the page we think
    it is". It goes to stderr because every command in this skill writes its
    JSON to stdout, so a warning here can never corrupt a result -- and because
    the alternative, a note nobody wired up, is the silence the whole module is
    written against.
    """
    print(f"discover: {message}", file=sys.stderr)


@dataclass
class Candidate:
    """A username someone suggested. Not yet a source -- nothing is verified."""

    username: str
    found_via: str
    context: str | None = None       # the text it appeared in, for query craft
    hits: int = 1
    # EVERY channel that produced this name, not only the first one to.
    # `found_via` keeps its meaning -- who said it first -- and this says who
    # else agreed. Each of the three channels is blind in its own way (the
    # account search cannot see inside a message, the web cannot see a group's
    # messages at all, lyzem's index is thin and a third of its names are dead),
    # so a name two of them independently produced is a better bet than a name
    # one of them mentioned twice, and the merge used to throw that away.
    channels: list = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.channels:
            self.channels = [self.found_via]

    def key(self) -> str:
        return self.username.lower()


NOT_A_SOURCE_REASON = "Telegram's own routing furniture, not a peer"


@dataclass
class DiscoveryResult:
    candidates: dict[str, Candidate] = field(default_factory=dict)
    channels_used: set = field(default_factory=set)
    notes: list[str] = field(default_factory=list)
    dropped: dict = field(default_factory=dict)   # reason -> how many

    def note(self, reason: str) -> None:
        """Count a rejection under a named reason, and say it once in `notes`.

        The module's own contract is that nothing is discarded silently -- a
        discovery stage that quietly threw away the good half would look exactly
        like one that found nothing. The `NOT_A_SOURCE` filter was the one place
        that returned early with no counter and no note.
        """
        self.dropped[reason] = self.dropped.get(reason, 0) + 1
        if reason not in self.notes:
            self.notes.append(reason)

    def add(self, cand: Candidate) -> None:
        key = cand.key()
        if key in NOT_A_SOURCE:
            self.note(f"{key}: {NOT_A_SOURCE_REASON}")
            return
        existing = self.candidates.get(key)
        if existing:
            # `+= cand.hits`, not `+= 1`: a candidate can arrive already carrying
            # a count -- a name a catalogue page mentions forty times is a much
            # better bet than one it mentions once, and adding one per arrival
            # threw that away.
            existing.hits += max(1, int(cand.hits))
            if not existing.context and cand.context:
                existing.context = cand.context
            for channel in cand.channels or [cand.found_via]:
                if channel not in existing.channels:
                    existing.channels.append(channel)
        else:
            self.candidates[key] = cand
        self.channels_used.add(cand.found_via)

    @property
    def corroborated(self) -> bool:
        """Have two channels of a different nature both spoken?"""
        return len(self.channels_used) >= 2

    def ranked(self) -> list[Candidate]:
        return sorted(self.candidates.values(), key=lambda c: (-c.hits, c.username.lower()))


# --------------------------------------------------------------------------
# Channel 1 -- lyzem, cross-channel message search
# --------------------------------------------------------------------------
LYZEM_SEARCH = "https://lyzem.com/search"

# The site's own name for the page-size control -- **hyphen, not underscore**.
#
# This was `per_page` for the life of the skill, and lyzem ignores an unknown
# key and serves its default of 10. Measured 2026-08-25 against the live page:
#
#     one common word            per_page=50 -> 10 blocks, 10 peers
#     f=channels                 per-page=50 -> 50 blocks, 50 peers
#     a three-word query         per_page=50 -> 10 blocks,  4 peers
#     f=messages                 per-page=50 -> 50 blocks, 33 peers
#
# So the one discovery channel that searches message text ACROSS channels was
# handing stage 2 twelve per cent of the candidates it was written to see, and
# stage 3 ten snippets to mine instead of fifty -- with `dropped: {}` and no
# note, in a module whose stated contract is that nothing is discarded
# silently. `lyzem_page_param` below is why it can never be silent again.
LYZEM_PER_PAGE_PARAM = "per-page"
LYZEM_PER_PAGE = 50

# A `<select>` whose options are a ladder of numbers is the page-size control,
# whatever it is called. Reading the name out of the page is what turns the next
# rename into a warning instead of a fourfold silent loss.
_SELECT_RE = re.compile(
    r"<select\b[^>]*\bname=[\"']?([A-Za-z0-9_-]+)[\"']?[^>]*>(.*?)</select>",
    re.I | re.S,
)
_OPTION_VALUE_RE = re.compile(r"<option\b[^>]*\bvalue=[\"']?(\d+)", re.I)


# The three lyzem modes stage 2 asks for, and why it is three rather than one.
#
# `f=messages` was the only mode this skill ever requested, and it is the one
# that answers the wrong question: it searches post TEXT, so a three-word query
# naming a city returned nothing about that city at all, while `f=groups` answered
# that city's own chat on the first line. The group and channel modes also
# carry a title and a description, which `f=messages` does not carry at all --
# in that mode even the result's own title anchor is empty, so a name found there
# cannot even be typed channel-or-group without another request. Measured
# 2026-08-25. Three modes is three GETs of a free surface and it is the cheapest
# repair in this stage.
LYZEM_KINDS = ("groups", "channels", "messages")


def lyzem_url(query: str, *, kind: str = "messages",
              per_page: int = LYZEM_PER_PAGE) -> str:
    return f"{LYZEM_SEARCH}?" + urllib.parse.urlencode(
        {"q": query, "f": kind, LYZEM_PER_PAGE_PARAM: per_page}
    )


def lyzem_page_param(body: str) -> str | None:
    """What lyzem's own search form calls its page-size control, or None.

    The live page publishes `<select name="per-page">` with options 10/25/50/100
    and marks none of them `selected`, so the page cannot say which size it
    served -- but it can and does say which parameter it listens to. That name,
    compared with the one we send, is the whole guard: a rename is loud on the
    first request after it happens instead of costing 88 % of the candidates for
    however long nobody notices.
    """
    for name, inner in _SELECT_RE.findall(body or ""):
        values = {int(v) for v in _OPTION_VALUE_RE.findall(inner)}
        if len(values) >= 2 and min(values) <= 25 and max(values) >= 50:
            return name
    return None


def parse_lyzem(body: str, query: str, *, asked_for: int | None = LYZEM_PER_PAGE,
                notes: list | None = None) -> tuple[list[Candidate], list[str], int | None]:
    """Return (candidates, message snippets, the count lyzem claims).

    The snippets matter as much as the usernames: they are text written by the
    people we are trying to find words from, and stage 3 mines them for jargon.

    `asked_for` is the page size `lyzem_url` requested, and passing it buys the
    three counters below. Every one of them is a way this parse can come back
    thin while looking exactly like a thin index -- which is the one conclusion
    the module's opening docstring forbids anybody to draw from a third-party
    service. Pass `notes` to collect them as strings; they go to stderr either
    way, because a counter nobody wired up is the silence itself.
    """
    body = body or ""
    root = tgdom.parse(body)
    candidates: list[Candidate] = []
    snippets: list[str] = []
    blocks_seen = 0
    blocks_without_peer = 0
    for block in root.find_all(cls="search-result"):
        blocks_seen += 1
        before = len(candidates)
        text = block.text()
        if text:
            snippets.append(text)
        # One result is ONE sighting. A lyzem result block carries the same
        # permalink twice -- once on the title anchor and once on the body
        # anchor -- so counting per href gave every lyzem name `hits: 2` against
        # `hits: 1` for anything lifted from a page. `ranked()` sorts by hits, so
        # the list the agent was told to verify first was entirely lyzem, the
        # channel SKILL.md itself calls thin and erratic. Measured on the saved
        # C20 page: 10 result blocks, 20 candidates, each name exactly twice.
        seen_here: set[str] = set()
        for node in block.walk():
            href = node.attrs.get("href", "") if node.attrs else ""
            m = USERNAME_RE.search(href)
            if m and m.group(1).lower() not in seen_here:
                seen_here.add(m.group(1).lower())
                candidates.append(
                    Candidate(m.group(1), "lyzem", context=text[:400])
                )
        if len(candidates) == before:
            blocks_without_peer += 1
    claimed = None
    m = re.search(r"([\d\s,]+)\s+results?", body, re.I)
    if m:
        digits = re.sub(r"\D", "", m.group(1))
        claimed = int(digits) if digits else None

    def _note(text: str) -> None:
        if notes is not None and text not in notes:
            notes.append(text)
        _warn(text)

    # 1. The parameter name. Cheapest and surest: the page names it itself.
    served_param = lyzem_page_param(body)
    if served_param and served_param != LYZEM_PER_PAGE_PARAM:
        _note(
            f"lyzem now calls its page-size parameter {served_param!r}; this build "
            f"asks with {LYZEM_PER_PAGE_PARAM!r}, so the request for "
            f"{asked_for} results was ignored and the page served its default. "
            f"{blocks_seen} result blocks came back. Fix LYZEM_PER_PAGE_PARAM"
        )
    # 2. The arithmetic, in case the control disappears entirely: a short page
    #    over an index that claims more is either a rename or a cap, and either
    #    way it is not "lyzem holds nothing".
    elif (asked_for is not None and blocks_seen < int(asked_for)
            and claimed is not None and claimed > blocks_seen):
        _note(
            f"asked lyzem for {asked_for} results and its page carried "
            f"{blocks_seen}, while it claims {claimed} in its index — the page "
            "size we send may be being ignored. This is a short page, NOT a thin "
            "index"
        )
    # 3. Blocks that parsed to no peer at all. `read.py` calls this
    #    `understood_nothing`, and it means the same thing here -- the permalink
    #    has moved out of the href and 50 blocks become 0 candidates with
    #    `dropped: {}`, which is indistinguishable from an empty index.
    if blocks_without_peer and blocks_without_peer == blocks_seen:
        _note(
            f"{blocks_seen} lyzem result blocks and not one of them carried a "
            "t.me link — the markup this parser reads has changed. That is a "
            "front-end change to report, NOT an empty index"
        )
    elif blocks_without_peer:
        _note(
            f"{blocks_without_peer} of {blocks_seen} lyzem result blocks carried "
            "no t.me link and yielded no candidate"
        )
    return candidates, snippets, claimed


# --------------------------------------------------------------------------
# Channel 2/3 -- names lifted out of any text (web results, catalogues, posts)
# --------------------------------------------------------------------------
def candidates_from_text(text: str, found_via: str, *, context: str | None = None,
                         dropped: list | None = None):
    """Every `t.me/<name>` and `@name` a blob of text mentions.

    This is how the web-search channel, the catalogue channel and the
    links-from-found-posts channel all deliver: an agent does the searching, the
    script does the extraction, and the extraction is identical in each case.

    Pass a list as `dropped` to be told, by name, what the `NOT_A_SOURCE` filter
    removed. Without it the filter is silent, which is the one thing this module
    says it never is -- `cmd_discover` collects one list across every text it
    reads and turns each entry into a counted `DiscoveryResult.note`.

    **A name mentioned five times comes back with `hits: 5`.** Repeats inside one
    text used to be suppressed outright, so a catalogue's headline group and a
    name it mentions once in a footer both arrived as `hits: 1` and `ranked()`
    -- which sorts by hits -- degenerated to alphabetical order. The agent was
    then asked to spend one GET per candidate verifying them, with no signal
    about which to try first.
    """
    out: list[Candidate] = []
    first: dict[str, Candidate] = {}
    for pattern in (USERNAME_RE, AT_RE):
        for match in pattern.finditer(text or ""):
            name = match.group(1)
            key = name.lower()
            if key in NOT_A_SOURCE:
                if dropped is not None:
                    dropped.append(f"{key}: {NOT_A_SOURCE_REASON}")
                continue
            seen = first.get(key)
            if seen is not None:
                seen.hits += 1
                continue
            cand = Candidate(name, found_via, context=context or _around(text, match))
            first[key] = cand
            out.append(cand)
    return out


def _around(text: str, match, width: int = 160) -> str:
    start = max(0, match.start() - width)
    end = min(len(text), match.end() + width)
    return text[start:end].strip()


# --------------------------------------------------------------------------
# Verification -- the one GET that decides everything
# --------------------------------------------------------------------------
def verify(web, username: str, *, save_dir_label: str | None = None) -> tgparse.PeerCard:
    """One `t.me/<name>` GET: does it exist, and is it a channel or a group?

    Every candidate passes through here before anything else happens to it. The
    rule the spec sets is absolute: **a name that has not passed this check may
    never go to a resolve**, because a resolve spent on a nonexistent name costs
    exactly as much as one that works, and the budget it spends from is the one
    that froze the account for ten hours.
    """
    label = save_dir_label or f"landing-{username}.html"
    resp = web.landing(username, save_as=label)
    card = tgparse.parse_landing(resp.body, username)
    if card.exists is False:
        card.title = None
        card.description = None
    return card


def probe_preview(web, username: str) -> bool:
    """Does `/s/` serve this name? Measured once, then remembered.

    Cheap to ask, and it saves one request on every later visit -- which is the
    entire reason the registry carries a `preview` field.
    """
    resp = web.preview(username)
    from tgweb import preview_available
    return preview_available(resp)


# --------------------------------------------------------------------------
# Admission
# --------------------------------------------------------------------------
@dataclass
class AdmissionReport:
    inserted: int = 0
    updated: int = 0
    rejected: int = 0
    reasons: dict = field(default_factory=dict)
    undecided_topics: list = field(default_factory=list)
    duplicates: int = 0      # the same name twice in one batch, merged not doubled
    # Things the operator must be told about a candidate that WAS admitted. A
    # rejection has always carried a sentence; an admission carried nothing but
    # a count, which is how a channel's read route got flipped by a command that
    # reported `updated: 1` and no more.
    warnings: list = field(default_factory=list)
    # The usernames actually written, in write order. A count cannot name
    # anybody, and the caller needs the names to say which candidates a run
    # vouched for.
    admitted: list = field(default_factory=list)

    def note(self, reason: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def warn(self, text: str) -> None:
        if text and text not in self.warnings:
            self.warnings.append(text)


def admit(
    registry: Registry,
    cards,
    *,
    rules: AdmissionRules,
    classifier: TopicClassifier | None = None,
    found_via: str = "manual",
    lang: str | None = None,
    geo: str | None = None,
) -> AdmissionReport:
    """Turn verified cards into registry lines, with a reason for every refusal.

    A discovery stage that silently discarded the good half looks exactly like
    one that found nothing, so nothing is discarded silently: every rejection is
    counted under a named reason and the counts go into the run report.
    """
    report = AdmissionReport()
    known = registry.load()
    to_write: list[Source] = []
    pending: dict[str, int] = {}     # username -> its place in `to_write`

    for card in cards:
        # A public entry point of this module does not hand a caller a bare
        # TypeError out of a damaged input. A card that is not a card is a
        # rejection with a named reason, which is what every other refusal in
        # this function is.
        try:
            data = card.as_dict() if hasattr(card, "as_dict") else dict(card)
        except (TypeError, ValueError):
            report.rejected += 1
            report.note(
                f"a {type(card).__name__} is not a peer card — `admit` takes "
                "what `verify` returns, or a mapping shaped like it"
            )
            continue
        username = (data.get("username") or "").lstrip("@")
        existing = known.get(username.lower())
        # `judge` has no positive test for existence: it refuses `exists is
        # False` and a `type` outside the known set, but a card that never
        # answered the question at all falls through both. A card that has not
        # been verified has no business in the registry, and the registry is
        # what decides whether a name may reach a resolve.
        if data.get("exists") is None:
            report.rejected += 1
            report.note(
                f"{username or '<no username>'}: never verified — the landing check "
                "left no verdict on whether this name exists"
            )
            continue
        data.setdefault("status", "alive" if data.get("exists") else "gone")
        verdict = judge(data, rules, existing)
        # The status the RULES decided, not the one the card guessed. `admit`
        # infers `gone` from `exists is False`, which is right for a name that
        # has vanished and wrong for one that went private: the name is still
        # taken, `judge` says so, and without this line the registry recorded
        # `gone` for a source that is merely closed. `VALID_STATUS` listed
        # `private` and nothing in the skill could produce it.
        if verdict.record_status:
            data["status"] = verdict.record_status
        for warning in verdict.warnings:
            report.warn(warning)
        if not verdict.admit:
            report.rejected += 1
            report.note(verdict.reason)
            continue

        topics, evidence = ([], {})
        if classifier:
            topics, evidence = classifier.classify(data.get("title"), data.get("description"))
        if not topics:
            report.undecided_topics.append(username)

        source = Source(
            username=username,
            type=data.get("type"),
            title=data.get("title"),
            description=data.get("description"),
            members=data.get("members"),
            topics=topics or (existing or {}).get("topics", []),
            lang=lang or (existing or {}).get("lang"),
            geo=geo or (existing or {}).get("geo"),
            found_via=found_via if not existing else (existing.get("found_via") or found_via),
            first_seen=(existing or {}).get("first_seen") or today_local(),
            preview=data.get("preview", (existing or {}).get("preview")),
            max_id_seen=(existing or {}).get("max_id_seen"),
            peer=(existing or {}).get("peer"),
            status=data.get("status", "unknown"),
            notes=("topics: " + "; ".join(f"{k}<-{','.join(v)}" for k, v in evidence.items()))
            if evidence else None,
            # Forwarded, never invented, and never read back off the stored
            # record -- `(existing or {})` is exactly the cache the rule forbids.
            #
            # `type` decides the whole read route, so `_merge` refuses to let a
            # contradicting check change it and records a `type_conflict` whose
            # note says "re-verify with type_confirmed to correct it".
            # `cmd_verify --write` is the one caller that can honestly say that:
            # it compares the transport's act counter either side of the landing
            # fetch and sets the flag only when the type came off a page fetched
            # in that same call. It then hands the card to `admit` -- which built
            # the `Source` without this field, dropped the flag on the floor, and
            # left the advice in the note unfollowable by the only command that
            # produces it. Measured: a source stored as `group`, re-verified as
            # `channel` with the flag set, came back `updated: 1`, a warning, a
            # written line carrying `type: channel` -- and a merged type still
            # `group`.
            #
            # A flag with no `type` beside it is a claim about nothing, so both
            # have to be on the card. Anything else -- a hand-written card, a
            # `--from-file` batch, a re-admission built from the registry -- has
            # no flag and gets none.
            type_confirmed=True if (data.get("type_confirmed")
                                    and data.get("type")) else None,
        )
        # The same name twice in one batch used to be appended twice and counted
        # twice: two lines on disk, `inserted=2` for one source. `Registry.load`
        # collapses them on read, so it was bookkeeping noise rather than
        # corruption -- but a count nobody can trust is not worth printing.
        seat = pending.get(username.lower())
        if seat is not None:
            to_write[seat] = source          # the later card wins
            report.duplicates += 1
            continue
        pending[username.lower()] = len(to_write)
        to_write.append(source)
        if verdict.action == "update":
            report.updated += 1
        else:
            report.inserted += 1

    registry.append_many(to_write)
    # The names that were really written, in the order they were written, after
    # the duplicate merge above has had its say. The caller was establishing the
    # same fact by loading the registry either side of this call and diffing --
    # exact, but it re-reads a file that only ever grows, twice, to learn
    # something `admit` knew all along. `inserted + updated` is a count and
    # cannot name anybody.
    report.admitted = [s.username for s in to_write]
    return report
