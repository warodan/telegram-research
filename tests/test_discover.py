"""discover.py -- stage 2, finding candidate channels and groups.

`admit()` writes through `registry.Registry`, so these tests use a real
Registry backed by `tmp_path`: a JSONL file on disk, exercised the same way
the CLI exercises it, just in a throwaway directory instead of the project's
own store.
"""

from __future__ import annotations

import json
from collections import Counter

import pytest

import discover
from registry import AdmissionRules, Registry


# --------------------------------------------------------------------------
# parse_lyzem -- C20, the only lyzem search result page in the corpus. It is an
# authored stand-in, not a capture: the saved page was a verbatim copy of
# somebody else's site listing real channels, so it was rebuilt to carry only
# the shape this parser reads -- the claimed count, the page-size control, and
# ten result blocks that each repeat their permalink on two anchors.
# --------------------------------------------------------------------------
def test_parse_lyzem_candidates_snippets_and_claimed_count(probe):
    body = probe("C20-lyzem-search.html")
    candidates, snippets, claimed = discover.parse_lyzem(body, "hanoi")

    assert claimed == 51
    assert len(snippets) == 10  # 10 result blocks on the saved page
    usernames = {c.username for c in candidates}
    assert len(usernames) == 10  # one candidate username per block
    assert {"example_channel_01", "example_group_02", "example_channel_04"} <= usernames
    assert all(c.found_via == "lyzem" for c in candidates)


def test_one_lyzem_result_is_one_sighting_not_two(probe):
    """Every lyzem candidate was counted twice, so lyzem always ranked first.

    A lyzem result block carries the same permalink twice -- on the title anchor
    and on the body anchor -- and the loop took every href in the block. The
    saved page produced 20 candidates from 10 results, each name exactly twice,
    so in any mixed command every lyzem name reported `hits: 2` against `hits: 1`
    for anything lifted from a page, and `ranked()` sorts by hits. The list the
    agent was told to verify first was entirely lyzem -- the channel `SKILL.md`
    itself calls thin and erratic -- and the printed number was simply wrong: two
    sightings where there was one.
    """
    body = probe("C20-lyzem-search.html")
    candidates, snippets, _ = discover.parse_lyzem(body, "hanoi")

    assert len(candidates) == len(snippets) == 10
    counted = Counter(c.username.lower() for c in candidates)
    assert set(counted.values()) == {1}

    result = discover.DiscoveryResult()
    for cand in candidates:
        result.add(cand)
    assert {c.hits for c in result.ranked()} == {1}


def test_parse_lyzem_on_unrelated_body_finds_nothing():
    candidates, snippets, claimed = discover.parse_lyzem("<html><body>nothing here</body></html>", "x")
    assert candidates == []
    assert snippets == []
    assert claimed is None


# --------------------------------------------------------------------------
# candidates_from_text
# --------------------------------------------------------------------------
def test_candidates_from_text_picks_up_both_forms_and_dedupes_case_insensitively():
    text = "Check t.me/HanoiChats and also @hanoichats again, plus t.me/durov"
    cands = discover.candidates_from_text(text, "web")
    usernames = [c.username for c in cands]
    # HanoiChats and @hanoichats are the same name in different casing and
    # different forms (t.me/x vs @x) -- only the first-seen spelling survives.
    assert usernames == ["HanoiChats", "durov"]
    assert all(c.found_via == "web" for c in cands)


def test_candidates_from_text_drops_not_a_source_names():
    text = "See t.me/telegram and t.me/durov and t.me/proxy for details"
    cands = discover.candidates_from_text(text, "web")
    usernames = {c.username.lower() for c in cands}
    assert "telegram" not in usernames
    assert "proxy" not in usernames
    assert "durov" in usernames


def test_candidates_from_text_empty_on_no_matches():
    assert discover.candidates_from_text("nothing interesting here", "web") == []


def test_every_official_link_form_is_recognised():
    """`telegram.me`, `telegram.dog` and `tg://resolve?domain=` were dropped.

    `USERNAME_RE` matched only the literal host `t.me`. `telegram.me` is
    Telegram's own alternative domain and is what older catalogue pages, forum
    posts and search results carry; `telegram.dog` is the third official alias;
    `tg://resolve?domain=` is what a great many web pages emit for the deep
    link. Measured on a catalogue-shaped blob naming nine peers: three were
    dropped with no counter and no note, and the web and catalogue channels of
    stage 2 are exactly the ones that carry those forms.
    """
    blob = (
        "join https://t.me/hanoi_chats and t.me/expatsinhanoi , @Hanoi_chat , "
        "https://telegram.me/vietnam_chatt , http://telegram.dog/danang16 , "
        "tg://resolve?domain=hanoi_forum , t.me/s/durov"
    )
    found = [c.username for c in discover.candidates_from_text(blob, "web")]

    assert set(found) == {"hanoi_chats", "expatsinhanoi", "Hanoi_chat",
                          "vietnam_chatt", "danang16", "hanoi_forum", "durov"}


@pytest.mark.parametrize("text", [
    "https://t.me/joinchat/AAAAAEabcdefg",
    "https://t.me/+iNvItEcOdE123",
    "https://t.me/c/1931920118/29327",
    "https://t.me/proxy?server=1.2.3.4&port=443",
    "write to someone@example.tld about it",
])
def test_the_forms_that_are_not_a_peer_stay_out(text):
    """The wider host list must not widen what counts as a name."""
    names = {c.username.lower() for c in discover.candidates_from_text(text, "web")}
    assert names <= {"joinchat", "proxy"} - discover.NOT_A_SOURCE
    assert names == set()


def test_a_name_mentioned_five_times_outranks_one_mentioned_once():
    """`hits` never counted repeats inside one text, so one `--from-file`
    produced an alphabetical ranking.

    `ranked()` sorts by `(-hits, username)`, and a name a catalogue mentions
    forty times arrived as `hits: 1` exactly like a name in its footer. The agent
    is then asked to spend one GET per candidate verifying them, with no signal
    about which to try first.
    """
    catalogue = (
        "t.me/hanoi_chats is the big one. t.me/hanoi_chats again, @hanoi_chats "
        "and https://t.me/hanoi_chats plus telegram.me/hanoi_chats -- "
        "t.me/expatsinhanoi once, and @admin_desk in the footer"
    )
    result = discover.DiscoveryResult()
    for cand in discover.candidates_from_text(catalogue, "web"):
        result.add(cand)
    ranked = [(c.username.lower(), c.hits) for c in result.ranked()]

    assert ranked[0] == ("hanoi_chats", 5)
    assert dict(ranked)["expatsinhanoi"] == 1
    assert dict(ranked)["admin_desk"] == 1


# --------------------------------------------------------------------------
# DiscoveryResult.corroborated
# --------------------------------------------------------------------------
def test_corroborated_false_with_one_channel():
    result = discover.DiscoveryResult()
    result.add(discover.Candidate("somechan1", "lyzem"))
    result.add(discover.Candidate("otherchan1", "lyzem"))  # same channel again
    assert result.corroborated is False


def test_corroborated_true_with_two_channels():
    result = discover.DiscoveryResult()
    result.add(discover.Candidate("somechan1", "lyzem"))
    result.add(discover.Candidate("somechan1", "web"))  # a different found_via
    assert result.corroborated is True


def test_discovery_result_add_merges_repeat_candidates():
    result = discover.DiscoveryResult()
    result.add(discover.Candidate("somechan1", "lyzem", context="first mention"))
    result.add(discover.Candidate("SomeChan1", "web"))  # same name, different case
    assert len(result.candidates) == 1
    cand = result.candidates["somechan1"]
    assert cand.hits == 2
    assert cand.context == "first mention"  # first non-empty context wins


# --------------------------------------------------------------------------
# admit -- insert, update, and a named reason for every rejection
# --------------------------------------------------------------------------
@pytest.fixture
def registry(tmp_path):
    return Registry(tmp_path / "registry.jsonl")


@pytest.fixture
def rules():
    return AdmissionRules()  # defaults: channel floor 100, group floor 50


def test_admit_inserts_and_updates(registry, rules):
    registry.append({"username": "existingchan1", "type": "channel", "members": 500, "status": "alive"})

    cards = [
        {"username": "existingchan1", "exists": True, "type": "channel", "members": 600},
        {"username": "newchannel1", "exists": True, "type": "channel", "members": 200},
    ]
    report = discover.admit(registry, cards, rules=rules, found_via="manual")

    assert report.inserted == 1
    assert report.updated == 1
    assert report.rejected == 0

    known = registry.load()
    assert known["existingchan1"]["members"] == 600
    assert known["newchannel1"]["members"] == 200


def test_admit_rejects_invalid_username(registry, rules):
    report = discover.admit(registry, [{"username": "ab", "exists": True, "type": "channel", "members": 1000}], rules=rules)
    assert report.rejected == 1
    assert report.inserted == 0
    assert any("not a valid Telegram username" in reason for reason in report.reasons)


def test_admit_rejects_nonexistent_name(registry, rules):
    report = discover.admit(registry, [{"username": "gonechannel1", "exists": False}], rules=rules)
    assert report.rejected == 1
    assert any("no such name" in reason for reason in report.reasons)


def test_admit_rejects_unknown_type(registry, rules):
    report = discover.admit(
        registry, [{"username": "typelesschannel", "exists": True, "type": None}], rules=rules
    )
    assert report.rejected == 1
    assert any("type is unknown" in reason for reason in report.reasons)


def test_admit_rejects_below_member_floor(registry, rules):
    report = discover.admit(
        registry,
        [{"username": "smallchannel1", "exists": True, "type": "channel", "members": 50}],
        rules=rules,
    )
    assert report.rejected == 1
    assert any("below the floor" in reason for reason in report.reasons)
    # nothing was written for the rejected candidate
    assert registry.get("smallchannel1") is None


def test_admit_never_writes_a_rejected_candidate(registry, rules):
    discover.admit(registry, [{"username": "ab", "exists": True, "type": "channel", "members": 1000}], rules=rules)
    assert registry.load() == {}


# --------------------------------------------------------------------------
# Nothing is discarded silently
# --------------------------------------------------------------------------
def test_routing_furniture_is_dropped_under_a_named_reason():
    """The module's own contract: "nothing is discarded silently: every
    rejection is counted under a named reason". `DiscoveryResult.add` returned
    early on NOT_A_SOURCE with no note and no counter, so four names in and one
    candidate out looked exactly like a channel that found nothing."""
    result = discover.DiscoveryResult()
    for name in ("telegram", "share", "proxy", "hanoi_chats"):
        result.add(discover.Candidate(name, "lyzem"))

    assert list(result.candidates) == ["hanoi_chats"]
    assert sum(result.dropped.values()) == 3
    assert len(result.notes) == 3
    assert any("telegram" in note for note in result.notes)
    assert all(discover.NOT_A_SOURCE_REASON in note for note in result.notes)


def test_candidates_from_text_can_report_what_it_filtered():
    dropped: list[str] = []
    text = "See t.me/telegram and t.me/durov and t.me/proxy for details"
    cands = discover.candidates_from_text(text, "web", dropped=dropped)

    assert [c.username for c in cands] == ["durov"]
    assert len(dropped) == 2
    assert any("telegram" in d for d in dropped)
    assert any("proxy" in d for d in dropped)


def test_a_drop_reported_by_name_reaches_the_result_as_a_counted_reason():
    """`discover` returns what it discarded and why, and `tg.py` prints it.

    This is the shape `cmd_discover` uses: one `dropped` list across every text,
    then one `note()` per reason, so `result.dropped` (a count per reason) and
    `result.notes` (each reason once) are what the CLI emits.
    """
    result = discover.DiscoveryResult()
    dropped: list[str] = []
    found = discover.candidates_from_text(
        "t.me/telegram t.me/share t.me/proxy t.me/durov and @addstickers",
        "web", dropped=dropped)
    for cand in found:
        result.add(cand)
    for reason in dropped:
        result.note(reason)

    assert [c.username for c in found] == ["durov"]
    assert sum(result.dropped.values()) == 4
    assert {"telegram", "share", "proxy", "addstickers"} == {
        note.split(":")[0] for note in result.notes
    }
    assert all(discover.NOT_A_SOURCE_REASON in note for note in result.notes)


# --------------------------------------------------------------------------
# A name twice in one batch is one source
# --------------------------------------------------------------------------
def test_the_same_card_twice_in_one_batch_is_written_once(registry, rules):
    card = {"username": "twicechannel1", "exists": True, "type": "channel", "members": 400}
    report = discover.admit(registry, [dict(card), dict(card)], rules=rules)

    assert report.inserted == 1
    assert report.duplicates == 1
    lines = [ln for ln in registry.path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1


def test_the_later_card_wins_when_a_name_repeats_in_one_batch(registry, rules):
    report = discover.admit(registry, [
        {"username": "twicechannel1", "exists": True, "type": "channel", "members": 400},
        {"username": "twicechannel1", "exists": True, "type": "channel", "members": 900},
    ], rules=rules)

    assert report.inserted == 1
    assert report.duplicates == 1
    assert registry.get("twicechannel1")["members"] == 900


# --------------------------------------------------------------------------
# A card that never said it exists cannot be admitted
# --------------------------------------------------------------------------
def test_admit_names_the_candidates_it_wrote(registry, rules):
    """`inserted + updated` is a count and cannot name anybody.

    The caller was establishing the same fact by loading the registry either
    side of `admit` and diffing -- exact, but it re-reads a file that only ever
    grows, twice, to learn something `admit` knew all along. The duplicate merge
    is included: a name given twice in one batch is written once and named once.
    """
    report = discover.admit(registry, [
        {"username": "firstchan1", "exists": True, "type": "channel", "members": 400},
        {"username": "smallchan1", "exists": True, "type": "channel", "members": 3},
        {"username": "twicechan1", "exists": True, "type": "channel", "members": 400},
        {"username": "twicechan1", "exists": True, "type": "channel", "members": 900},
    ], rules=rules)

    assert report.admitted == ["firstchan1", "twicechan1"]
    assert len(report.admitted) == report.inserted + report.updated
    assert "smallchan1" not in report.admitted        # rejected, and not named
    assert set(report.admitted) == set(registry.load())


def test_admit_names_nobody_when_it_wrote_nobody(registry, rules):
    report = discover.admit(
        registry, [{"username": "ab", "exists": True, "type": "channel", "members": 1000}],
        rules=rules)
    assert report.admitted == []
    assert registry.load() == {}


def test_a_source_that_went_private_is_recorded_as_private_not_gone(registry, rules):
    """`admit` inferred the status from `exists`, so `private` was unreachable.

    `VALID_STATUS` listed both `gone` and `private` and nothing in the skill
    could produce the second: a name that is still TAKEN but no longer serves a
    readable peer was written as `gone`, which is a different fact. `judge` knows
    the difference (it reads `taken`); this is the half that records it.
    """
    registry.append({"username": "closedchan1", "type": "group", "members": 900,
                     "status": "alive", "max_id_seen": 77})
    registry.append({"username": "goneychan1", "type": "channel", "members": 900,
                     "status": "alive"})

    report = discover.admit(registry, [
        {"username": "closedchan1", "exists": False, "taken": True},
        {"username": "goneychan1", "exists": False, "taken": False},
    ], rules=rules)

    known = registry.load()
    assert known["closedchan1"]["status"] == "private"
    assert known["goneychan1"]["status"] == "gone"
    # The record keeps what it was worth: the cursor survives the status change.
    assert known["closedchan1"]["max_id_seen"] == 77
    assert report.updated == 2 and report.rejected == 0


def test_an_admitted_candidate_can_still_carry_a_warning(registry, rules):
    """A rejection was a sentence; an admission was a count and nothing else.

    That is how a change to a source the registry already holds reached the
    operator as `updated: 1` and no more.
    """
    registry.append({"username": "closedchan1", "type": "group", "members": 900,
                     "status": "alive"})
    report = discover.admit(
        registry, [{"username": "closedchan1", "exists": False, "taken": True}],
        rules=rules)

    assert report.updated == 1
    assert any("private" in w and "closedchan1" in w for w in report.warnings)


def test_a_card_with_no_verdict_on_existence_is_refused_by_name(registry, rules):
    """`judge` refuses `exists is False` and an unknown type, but a card that
    never answered the question at all fell through both. The registry is what
    decides whether a name may reach a resolve, so an unverified name has no
    business in it."""
    report = discover.admit(
        registry, [{"username": "unreadable1", "type": "channel", "members": 900}],
        rules=rules,
    )

    assert report.inserted == 0
    assert report.rejected == 1
    assert any("never verified" in reason for reason in report.reasons)
    assert registry.load() == {}


# ==========================================================================
# The lyzem page-size control, and a block that carries no peer link
# ==========================================================================
# The page-size control as lyzem really serves it, copied from the live page on
# 2026-08-25. It marks no option `selected`, so the page cannot say which size
# it served -- but it says which parameter it listens to, and that is the guard.
LYZEM_SELECT = (
    '<select name="per-page">'
    '<option value="10">10</option><option value="25">25</option>'
    '<option value="50">50</option><option value="100">100</option>'
    "</select>"
)


def _lyzem_page(blocks: int, *, select: str = LYZEM_SELECT, claimed: int = 1798,
                peer_link: bool = True) -> str:
    body = [f"<html><body><form>{select}</form><div>{claimed} results</div>"]
    for i in range(blocks):
        href = (f'<a href="https://t.me/chan{i}/{i + 100}">chan{i}</a>'
                if peer_link else '<a href="/redirect?to=hidden">open</a>')
        body.append(f'<div class="search-result">{href}<p>post text {i}</p></div>')
    body.append("</body></html>")
    return "".join(body)


def test_lyzem_is_asked_with_the_parameter_the_site_actually_listens_to():
    """Reproduced live 2026-08-25 against `lyzem.com/search`:

        one common word, f=channels   per_page=50 -> 10 blocks, 10 unique peers
                                      per-page=50 -> 50 blocks, 50 unique peers
        a three-word query, f=messages per_page=50 -> 10 blocks,  4 unique peers
                                      per-page=50 -> 50 blocks, 33 unique peers

    lyzem ignores an unknown key and serves its default of 10, so the one
    discovery channel that searches message text across channels handed stage 2
    twelve per cent of the candidates it was written to see -- and stage 3 ten
    snippets to mine instead of fifty. The top-ranked candidate for a question
    about one city was a channel for a different city a thousand miles away.
    """
    url = discover.lyzem_url("аренда квартиры")
    assert "per-page=50" in url
    assert "per_page" not in url
    assert discover.LYZEM_PER_PAGE_PARAM == "per-page"


def test_the_page_size_parameter_is_read_back_off_the_page_itself():
    assert discover.lyzem_page_param(_lyzem_page(2)) == "per-page"
    # A select that is not a page-size ladder is not mistaken for one.
    assert discover.lyzem_page_param(
        '<select name="sort"><option value="1">new</option></select>') is None
    assert discover.lyzem_page_param("") is None


def test_a_renamed_page_size_parameter_is_named_and_counted_not_silent(capsys):
    """The counter that makes the next rename loud on its first request.

    Nothing counted the loss when this happened: `dropped: {}`, ten blocks, and
    a module whose stated contract is that nothing is discarded silently.
    """
    renamed = LYZEM_SELECT.replace('name="per-page"', 'name="results-per-page"')
    notes: list[str] = []
    cands, snippets, claimed = discover.parse_lyzem(
        _lyzem_page(10, select=renamed), "vpn", asked_for=50, notes=notes)

    assert len(cands) == 10 and claimed == 1798
    assert notes, "a rename that costs 80% of the candidates was not counted"
    said = " ".join(notes)
    assert "results-per-page" in said and "per-page" in said
    assert "LYZEM_PER_PAGE_PARAM" in said
    assert "discover:" in capsys.readouterr().err


def test_a_short_page_over_a_large_index_is_not_reported_as_a_thin_index():
    """The arithmetic backstop, for the day the control disappears entirely.

    "Lyzem found nothing" means "its index holds nothing", and this module's
    opening docstring forbids any other reading -- so a page that came back
    short while the index claims thousands must say which of the two it is.
    """
    notes: list[str] = []
    discover.parse_lyzem(_lyzem_page(10, select=""), "vpn",
                         asked_for=50, notes=notes)
    said = " ".join(notes)
    assert "10" in said and "1798" in said
    assert "NOT a thin index" in said


def test_a_full_page_of_the_size_asked_for_says_nothing_at_all(capsys):
    notes: list[str] = []
    cands, _, _ = discover.parse_lyzem(_lyzem_page(50), "vpn",
                                       asked_for=50, notes=notes)
    assert len(cands) == 50
    assert notes == []
    assert capsys.readouterr().err == ""


def test_result_blocks_that_carry_no_peer_link_are_counted(capsys):
    """Reproduced here rather than left as a read-only observation.

    `parse_lyzem` walks each block for an href matching `USERNAME_RE` and
    appends nothing when none is found. A lyzem markup change that moves the
    permalink -- into a `data-` attribute, or behind an interstitial -- turns 50
    blocks into 0 candidates with `dropped: {}` and no note, which is
    indistinguishable from "lyzem's index holds nothing". `read.py` already has
    the pattern for this and calls it `understood_nothing`.
    """
    notes: list[str] = []
    cands, snippets, _ = discover.parse_lyzem(
        _lyzem_page(50, peer_link=False), "vpn", asked_for=50, notes=notes)

    assert cands == []
    assert len(snippets) == 50, "the blocks were there and were read"
    said = " ".join(notes)
    assert "50 lyzem result blocks" in said
    assert "front-end change" in said and "NOT an empty index" in said
    assert "discover:" in capsys.readouterr().err


def test_some_blocks_without_a_peer_link_are_counted_too():
    notes: list[str] = []
    mixed = _lyzem_page(4).replace(
        "</body>",
        '<div class="search-result"><a href="/x">no peer</a></div></body>')
    discover.parse_lyzem(mixed, "vpn", asked_for=50, notes=notes)
    assert any("1 of 5" in note for note in notes), notes


# --------------------------------------------------------------------------
# A public entry point does not hand back a bare TypeError
# --------------------------------------------------------------------------
def test_parse_lyzem_survives_a_body_that_is_not_a_page():
    assert discover.parse_lyzem(None, "vpn") == ([], [], None)
    assert discover.parse_lyzem("", "vpn") == ([], [], None)


def test_admit_refuses_a_thing_that_is_not_a_card_with_a_named_reason(tmp_path):
    """`dict(card)` on anything unmappable was a bare TypeError out of `admit`.
    Every other refusal in this function is counted under a reason; so is this
    one now."""
    registry = Registry(tmp_path / "sources.jsonl")
    report = discover.admit(registry, [42, "durov", None],
                            rules=AdmissionRules())
    assert report.rejected == 3
    assert report.inserted == 0
    assert any("not a peer card" in reason for reason in report.reasons)


# --------------------------------------------------------------------------
# A confirmed type has to survive `admit` to reach the registry line
# --------------------------------------------------------------------------
def _stored(tmp_path, **fields):
    reg = Registry(tmp_path / "sources.jsonl")
    reg.append({"username": "chan", "exists": True, "status": "alive", **fields})
    return reg


def _card(**fields):
    return {"username": "chan", "exists": True, "type": "channel", "title": "T",
            "members": 5000, **fields}


def test_a_confirmed_type_reaches_the_registry_line_through_admit(tmp_path):
    """`_merge` refuses to let a contradicting check change `type` and records a
    `type_conflict` whose note says "re-verify with type_confirmed to correct
    it". `cmd_verify --write` is the only caller that can honestly say that --
    it compares the transport's act counter either side of the landing fetch --
    and it hands the card to `admit`, which built its `Source` without the field
    and dropped the flag.

    Measured before the repair: a source stored as `group`, re-verified as
    `channel` with the flag set, came back `updated: 1`, a warning, a written
    line carrying `type: channel` -- and a merged type still `group`. The advice
    in the conflict note was unfollowable by the one command that produces it,
    so a source admitted with a wrong type stayed wrong for ever.
    """
    reg = _stored(tmp_path, type="group")
    report = discover.admit(reg, [_card(type_confirmed=True)],
                            rules=AdmissionRules())

    assert report.updated == 1
    written = json.loads(
        (tmp_path / "sources.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert written["type_confirmed"] is True
    merged = reg.load()["chan"]
    assert merged["type"] == "channel", "the correction must land"
    assert "type_conflict" not in merged


def test_without_the_flag_the_stored_type_still_stands(tmp_path):
    """The other side of the same rule: an ordinary admission may not change the
    field that decides the read route."""
    reg = _stored(tmp_path, type="group")
    discover.admit(reg, [_card()], rules=AdmissionRules())

    merged = reg.load()["chan"]
    assert merged["type"] == "group"
    assert merged["type_conflict"]["seen"] == "channel"
    written = json.loads(
        (tmp_path / "sources.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert "type_confirmed" not in written


def test_admit_never_takes_the_flag_off_the_stored_record(tmp_path):
    """The whole point of the flag: it says the type was read from a page fetched in
    THAT call. A flag sitting on a line in the registry is a record of a fetch
    somebody else made, at some other time -- reading it back would be exactly
    the cache the rule forbids, and it would let any later card change the read
    route for free."""
    reg = _stored(tmp_path, type="group", type_confirmed=True)
    assert reg.load()["chan"].get("type_confirmed") is True, "the stale flag is there"

    discover.admit(reg, [_card()], rules=AdmissionRules())

    merged = reg.load()["chan"]
    assert merged["type"] == "group", "a stale flag must not license a change"
    assert merged["type_conflict"]["stored"] == "group"


def test_a_false_flag_is_not_written_as_a_claim(tmp_path):
    """`false` on a line is noise that says nothing `_merge` does not already
    assume, and `registry._prepare` pops it -- but it must not be built in the
    first place, because a claim about evidence is either true or absent."""
    reg = Registry(tmp_path / "sources.jsonl")
    card = {"username": "chan", "exists": True, "type": "channel",
            "members": 5000, "type_confirmed": False}
    discover.admit(reg, [card], rules=AdmissionRules())
    written = json.loads(
        (tmp_path / "sources.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert "type_confirmed" not in written


def test_a_card_with_no_type_never_reaches_the_registry_at_all(tmp_path):
    """Why `admit` also requires `type` beside the flag, and why that clause
    cannot be reached: `judge` refuses a typeless card before the `Source` is
    built, so a flag with nothing to confirm can never be written. The clause
    stays as a guard at the field's own site rather than a dependence on a check
    three functions away -- and this test records that the outer refusal is what
    really holds."""
    reg = Registry(tmp_path / "sources.jsonl")
    card = {"username": "chan", "exists": True, "members": 5000,
            "type_confirmed": True}
    report = discover.admit(reg, [card], rules=AdmissionRules())

    assert report.inserted == 0 and report.rejected == 1
    assert any("type is unknown" in reason for reason in report.reasons)
    assert not (tmp_path / "sources.jsonl").exists()
