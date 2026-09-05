"""Tests for scripts/querycraft.py: the jargon-mining loop and its three stoppers.

No network, no installs. Everything here runs against in-memory message dicts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = (Path(__file__).resolve().parent.parent
           / "skills" / "telegram-research" / "scripts")
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import querycraft
from querycraft import QueryLog, QueryLogError, fold


def test_fold_is_public_because_another_module_depends_on_it():
    """`tg.py accept` has to fold a term exactly the way the corpus was folded.

    Whether the drift ban admits a term is decided by comparing folded strings,
    so a caller reaching for a private `_fold` -- or worse, falling back to its
    own `casefold()` -- is a divergence that does not fail loudly: it lets a
    drifting word through the one guard that exists to stop it.
    """
    assert callable(fold)
    assert querycraft._fold is fold, "the old private name must not be a second rule"

    assert fold("  ЕЩЁ Раз  ") == "еще раз"
    assert fold("Straße") == "strasse"
    assert fold(fold("ЕЩЁ")) == fold("ЕЩЁ")    # idempotent
    assert fold(None) == "" and fold("") == ""

    # And the fact that matters: a term folded by a caller is the term the ban
    # and the miner see.
    log = QueryLog()
    log.record_posts([{"url": "u1", "text": "ещё раз про депозит"}])
    assert fold("ЕЩЁ") in log.corpus_tokens
    assert log.allows(fold("Еще"))[0] is True


# --------------------------------------------------------------------------
# Stopper 1: round ceiling
# --------------------------------------------------------------------------
def test_round_ceiling_stops_the_loop_and_says_why():
    log = QueryLog(max_rounds=2, min_new_posts=1)

    log.start_round(["q1"])
    log.record_posts([{"url": "u1", "text": "hello world"}])
    ok, _ = log.may_continue()
    assert ok is True  # only 1 of 2 rounds spent so far

    log.start_round(["q2"])
    log.record_posts([{"url": "u2", "text": "hello world"}])
    ok, why = log.may_continue()
    assert ok is False
    assert "round ceiling of 2" in why


# --------------------------------------------------------------------------
# Stopper 2: the yield floor
# --------------------------------------------------------------------------
def test_yield_floor_stops_after_a_round_below_the_floor():
    log = QueryLog(max_rounds=5, min_new_posts=3)
    log.start_round(["q1"])
    log.record_posts([{"url": f"u{i}", "text": "hello"} for i in range(2)])  # 2 < floor of 3
    ok, why = log.may_continue()
    assert ok is False
    assert "below the floor of 3" in why


def test_yield_floor_does_not_stop_on_a_round_merely_small_but_above_it():
    log = QueryLog(max_rounds=5, min_new_posts=3)
    log.start_round(["q1"])
    log.record_posts([{"url": f"u{i}", "text": "hello"} for i in range(4)])  # 4 >= floor of 3
    ok, _ = log.may_continue()
    assert ok is True


# --------------------------------------------------------------------------
# Stopper 3: the drift ban
# --------------------------------------------------------------------------
def test_drift_ban_allows_a_term_found_verbatim_in_retrieved_text():
    log = QueryLog()
    log.start_round(["seed"])
    log.record_posts([{"url": "u1", "text": "passed the driving test po rahmetu somehow"}])
    ok, why = log.allows("rahmetu")
    assert ok is True
    assert "verbatim" in why


def test_drift_ban_refuses_a_plausible_term_absent_from_every_post():
    log = QueryLog()
    log.start_round(["seed"])
    log.record_posts([{"url": "u1", "text": "passed the driving test somehow"}])
    ok, why = log.allows("vzyatka")
    assert ok is False
    assert "refused as drift" in why


# --------------------------------------------------------------------------
# candidates(): frequency-ranked but gated by min_documents
# --------------------------------------------------------------------------
def test_candidates_requires_min_documents_so_one_repeater_cannot_outrank_the_community():
    messages = [
        # one person, one post, the word repeated ten times: high frequency, one document.
        {"url": "u1", "text": " ".join(["repeatword"] * 10)},
    ] + [
        # six different posts, the word used once each: lower raw frequency, six documents.
        {"url": f"u{i}", "text": "communityword shows up here"} for i in range(2, 8)
    ]
    log = QueryLog()
    log.start_round(["seed"])
    candidates = log.candidates(messages, min_documents=2)
    terms = {c.term: c for c in candidates}

    assert "repeatword" not in terms  # frequency 10, but only 1 document -- below the floor
    assert "communityword" in terms
    assert terms["communityword"].frequency == 6
    assert terms["communityword"].documents == 6


# --------------------------------------------------------------------------
# accept() carries round + examples; to_markdown() renders everything
# --------------------------------------------------------------------------
def test_accepted_terms_carry_round_and_examples_and_to_markdown_renders_all():
    log = QueryLog(max_rounds=5, min_new_posts=1)

    log.start_round(["seed query"])
    round1_posts = [{"url": "https://t.me/x/1", "text": "rahmet appears in this post"}]
    log.record_posts(round1_posts)
    candidates = log.candidates(round1_posts, min_documents=1)
    term = next(c for c in candidates if c.term == "rahmet")
    accepted = log.accept(term, gloss="euphemism for a bribe")

    assert accepted.round_found == 1
    assert accepted.examples
    assert accepted.examples[0][0] == "https://t.me/x/1"

    log.start_round(["rahmet"])
    log.record_posts([{"url": "https://t.me/x/2", "text": "another rahmet mention"}])

    md = log.to_markdown()
    assert "## Round 1" in md
    assert "## Round 2" in md
    assert "## Vocabulary mined from the corpus" in md
    assert "rahmet" in md
    assert "euphemism for a bribe" in md


# --------------------------------------------------------------------------
# The drift ban keys on the corpus, not on the round ledger
# --------------------------------------------------------------------------
def test_drift_ban_is_on_as_soon_as_a_corpus_exists_even_with_no_round_started():
    """The natural order of work is: pick the queries, check them, THEN start
    the round. Keying the ban on `self.rounds` meant every batch was checked in
    the one state where the ban was off."""
    log = QueryLog()
    log.record_posts([{"url": "u1", "text": "аренда квартиры в центре"}])
    assert log.rounds == []                      # nothing started yet

    ok, why = log.allows("взятка")
    assert ok is False
    assert "refused as drift" in why

    ok, _ = log.allows("аренда")
    assert ok is True


def test_the_seed_query_is_still_allowed_while_the_corpus_is_empty():
    log = QueryLog()
    ok, why = log.allows("аренда квартиры")
    assert ok is True
    assert "seed" in why


# --------------------------------------------------------------------------
# The drift ban matches words, not substrings
# --------------------------------------------------------------------------
@pytest.fixture
def rented():
    log = QueryLog()
    log.record_posts([{"url": "u1", "text": "аренда квартиры в центре, по рахмету"}])
    return log


@pytest.mark.parametrize("query", ["о", "а", "нда кварти"])
def test_drift_ban_refuses_fragments_that_a_substring_test_admitted(rented, query):
    """`allows` was a naked substring test over the folded corpus, so a single
    common letter and a fragment spanning a word boundary both passed a ban
    whose stated job is to keep the run on subject."""
    ok, why = rented.allows(query)
    assert ok is False, f"{query!r} was admitted"
    assert why


@pytest.mark.parametrize("query", ["рахмету", "РАХМЕТУ", "Рахмету", "аренда квартиры"])
def test_drift_ban_still_admits_real_corpus_words(rented, query):
    ok, _ = rented.allows(query)
    assert ok is True


def test_drift_ban_admits_an_inflected_stem_of_a_corpus_word(rented):
    """Russian jargon arrives inflected: the corpus says «рахмету» and the
    query worth running is «рахмет». A strict word match would refuse exactly
    the leads the mining stage exists to produce."""
    ok, why = rented.allows("рахмет")
    assert ok is True
    assert "stem" in why


def test_drift_ban_refuses_a_stem_too_short_to_mean_anything(rented):
    ok, why = rented.allows("ар")
    assert ok is False
    assert "floor" in why


# --------------------------------------------------------------------------
# The drift ban is a PHRASE ban
# --------------------------------------------------------------------------
@pytest.fixture
def two_posts():
    """Two posts sharing no phrase, which is the whole point of them."""
    log = QueryLog()
    log.record_posts([
        {"url": "u1", "text": "риелтор просит депозит за студию"},
        {"url": "u2", "text": "сдал по рахмету"},
    ])
    return log


@pytest.mark.parametrize("query", ["рахмету студию", "депозит рахмету студию",
                                   "студию рахмету"])
def test_a_query_assembled_out_of_two_posts_is_refused(two_posts, query):
    """`SKILL.md` says a query must appear VERBATIM in text already retrieved.

    Word-by-word matching admitted every one of these and returned the sentence
    "found verbatim in retrieved text" about a phrase that occurs in no post --
    the drift the ban exists to stop, arriving with a certificate saying it is
    not drift. Measured through `tg.py queries start`, which printed
    `{"allowed": true, "why": "found verbatim in retrieved text"}`.
    """
    ok, why = two_posts.allows(query)
    assert ok is False, f"{query!r} was admitted: {why}"
    assert "refused as drift" in why
    assert "verbatim" not in why


@pytest.mark.parametrize("query", ["риелтор просит", "просит депозит",
                                   "депозит за студию", "рахмету"])
def test_a_phrase_that_really_stands_in_one_post_is_admitted(two_posts, query):
    ok, why = two_posts.allows(query)
    assert ok is True, f"{query!r} was refused: {why}"


def test_a_short_word_inside_a_query_is_ignored_not_fatal():
    """`visa on arrival` was refused whole because `on` is two letters.

    Every token had to clear the three-letter floor or the query was refused BY
    NAME before any corpus test ran -- so from round 2 onward the ban blocked
    ordinary English phrase search, and `аренда OR квартиры` with it, which is
    the opposite of the risk the floor was put there for. The floor belongs on
    the words being checked, not on the query carrying them.
    """
    log = QueryLog()
    log.record_posts([
        {"url": "u1", "text": "the visa on arrival queue was long"},
        {"url": "u2", "text": "visa on arrival is easy at the airport"},
    ])

    ok, why = log.allows("visa on arrival")
    assert ok is True
    # Keeping the short words licensed this change of wording. `on` is no longer *ignored*:
    # the corpus sequence keeps its short words, so the phrase matched position
    # for position and the word "verbatim" is now literally true when it is
    # used. It was not before -- what was checked was the query with its short
    # words deleted.
    assert "verbatim" in why

    # The phrase test still runs on what is left, so word order still matters.
    assert log.allows("arrival on visa")[0] is False

    ru = QueryLog()
    ru.record_posts([{"url": "u1", "text": "аренда квартиры в центре"}])
    assert ru.allows("аренда OR квартиры")[0] is True


def test_a_query_of_nothing_but_short_words_is_still_refused():
    log = QueryLog()
    log.record_posts([{"url": "u1", "text": "аренда квартиры в центре"}])
    ok, why = log.allows("в на")
    assert ok is False
    assert "floor" in why


def test_yo_and_ye_are_the_same_word():
    """ё and е are separate code points that NFKC does not merge, and Russian
    writes the same word both ways. The ban called one of them drift."""
    log = QueryLog()
    log.record_posts([{"url": "u1", "text": "ещё раз про депозит"}])

    assert log.allows("ещё")[0] is True
    assert log.allows("еще")[0] is True, "a spelling variant was called drift"

    # And the other direction: an е-spelled corpus admits the ё-spelled query.
    other = QueryLog()
    other.record_posts([{"url": "u1", "text": "еще раз про депозит"}])
    assert other.allows("ещё")[0] is True
    assert other.allows("ещё раз")[0] is True


def test_a_log_written_before_yo_folding_is_folded_on_the_way_in(tmp_path):
    """A saved corpus outlives the rule, so the rule is applied on load."""
    log = QueryLog()
    log.record_posts([{"url": "u1", "text": "ещё раз про депозит"}])
    state = log.to_state()
    # What a pre-fix file holds: the ё spelling, in the text and in the tokens.
    state["corpus_text"] = ["ещё раз про депозит"]
    state["corpus_tokens"] = ["ещё", "раз", "про", "депозит"]
    (tmp_path / "queries.json").write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8")

    back = QueryLog.load(tmp_path / "queries.json")
    assert back.allows("еще")[0] is True
    assert back.allows("еще раз")[0] is True


# --------------------------------------------------------------------------
# candidates() and allows() must not contradict each other
# --------------------------------------------------------------------------
def test_every_term_candidates_mines_is_a_term_allows_admits():
    """`candidates()` read a batch and did not put it in the corpus, so the ban
    refused the very words the mining stage had just found in it:
    mined ['цена', 'жетонах', 'студию'] -> allows('жетонах') = False."""
    log = QueryLog()
    log.start_round(["seed"])
    log.record_posts([{"url": "seed1", "text": "первый пост про жильё"}])

    batch = [
        {"url": "https://t.me/x/1", "text": "снимаю студию за 300 жетонов"},
        {"url": "https://t.me/x/2", "text": "студию сдают за жетонов немного"},
    ]
    mined = log.candidates(batch, min_documents=2)
    assert mined, "nothing was mined -- the test proves nothing"

    for term in mined:
        ok, why = log.allows(term.term)
        assert ok is True, f"{term.term!r} was mined and then refused: {why}"


# --------------------------------------------------------------------------
# `record` excludes the WORDS of every query already used
# --------------------------------------------------------------------------
def test_a_multi_word_query_is_excluded_by_its_words_not_only_whole():
    """The shortlist handed back the words of the round just spent, ranked first.

    `excluded` folded each exclusion as a whole string, and `tg.py record` passes
    whole query strings into it (`run.brief.seed_queries`, every round's
    queries), so a multi-word query never matched any single token. Measured:
    `exclude=['аренда квартиры']` -> `['аренда', 'квартиры', 'депозит']`, in a
    stage whose stated purpose is to find what the question could NOT have said,
    and every round spent re-searching them is a round off the ceiling.
    """
    posts = [
        {"url": "u1", "text": "аренда квартиры и депозит"},
        {"url": "u2", "text": "аренда квартиры без депозит"},
    ]
    log = QueryLog()
    log.record_posts(posts)

    mined = [c.term for c in log.candidates(posts, exclude=["аренда квартиры"],
                                            min_documents=2)]
    assert "аренда" not in mined
    assert "квартиры" not in mined
    assert "депозит" in mined, "the exclusion ate a word it was not given"

    # A single-word exclusion kept working, and still does.
    single = [c.term for c in log.candidates(posts, exclude=["аренда"],
                                             min_documents=2)]
    assert "аренда" not in single and "квартиры" in single


def test_the_question_and_every_round_are_excluded_by_word():
    """The shape `tg.py record` really calls this with."""
    posts = [{"url": f"u{i}", "text": "сколько стоит аренда студии в центре"}
             for i in range(3)]
    log = QueryLog()
    log.record_posts(posts)
    mined = [c.term for c in log.candidates(
        posts, exclude=["аренда студии", "сколько стоит аренда"], min_documents=2)]

    assert not ({"аренда", "студии", "сколько", "стоит"} & set(mined))
    assert "центре" in mined


# --------------------------------------------------------------------------
# _snippet -- the evidence a gloss is judged by  (finding 19)
# --------------------------------------------------------------------------
def test_a_snippet_is_cut_where_the_term_really_is():
    """The index was computed in the folded text and used to slice the original.

    `casefold` and NFKC are not length-preserving -- `ß` folds to `ss` -- so the
    snippet stored as the evidence for a mined term came back shifted by one
    character per fold, and with enough of them the term is not in its own
    snippet at all.
    """
    from querycraft import _snippet

    text = "ß" * 20 + " риелтор берёт комиссию и ещё что-то"
    snippet = _snippet(text, "риелтор", width=20)
    assert "риелтор" in snippet, "the term fell outside its own snippet"

    # The folded form of the term is what `candidates()` passes in, so a ё in
    # the original must still be found.
    assert "берёт" in _snippet("Straße риелтор берёт комиссию", "берет", width=30)

    # A term that is not there at all still returns the head of the text.
    assert _snippet("короткий текст", "отсутствует", width=8) == "короткий"


def test_a_mined_term_is_shown_the_post_it_came_from():
    log = QueryLog()
    posts = [{"url": "https://t.me/x/1", "text": "Straße и риелтор берёт комиссию"},
             {"url": "https://t.me/x/2", "text": "риелтор снова берёт своё"}]
    log.record_posts(posts)
    term = next(c for c in log.candidates(posts, min_documents=2)
                if c.term == "риелтор")

    assert term.examples
    for url, snippet in term.examples:
        assert url.startswith("https://t.me/x/")
        assert "риелтор" in snippet


# --------------------------------------------------------------------------
# save / load -- the round trip the CLI needs  (contract item 4)
# --------------------------------------------------------------------------
def _worked_log() -> QueryLog:
    log = QueryLog(max_rounds=4, min_new_posts=2)
    log.start_round(["аренда"])
    posts = [
        {"url": "https://t.me/x/1", "text": "аренда студии, 300 жетонов"},
        {"url": "https://t.me/x/2", "text": "студия сдаётся, жетонов немного"},
    ]
    log.record_posts(posts)
    term = next(c for c in log.candidates(posts, min_documents=2) if c.term == "жетонов")
    log.accept(term, gloss="đồng, the local currency")
    log.rounds[-1].stopped_because = "yield floor"
    log.start_round(["жетонов"])
    log.record_posts([{"url": "https://t.me/x/3", "text": "ещё жетонов за студию"}])
    return log


def test_query_log_round_trips_through_disk(tmp_path):
    log = _worked_log()
    path = log.save(tmp_path / "queries.json")
    assert path.exists()

    back = QueryLog.load(tmp_path / "queries.json")

    assert back.to_state() == log.to_state()
    assert back.max_rounds == 4 and back.min_new_posts == 2
    assert [r.number for r in back.rounds] == [1, 2]
    assert back.rounds[0].stopped_because == "yield floor"
    assert back.rounds[1].new_posts == 1
    assert back.seen_post_urls == log.seen_post_urls
    assert back.to_markdown() == log.to_markdown()


def test_a_reloaded_log_still_enforces_the_drift_ban(tmp_path):
    """The corpus travels with the log. A log reloaded without it would answer
    "no corpus yet -- the question itself is the seed" to everything."""
    _worked_log().save(tmp_path / "queries.json")
    back = QueryLog.load(tmp_path / "queries.json")

    assert back.allows("жетонов")[0] is True
    assert back.allows("взятка")[0] is False


def test_a_reloaded_term_keeps_its_gloss_and_its_examples(tmp_path):
    _worked_log().save(tmp_path / "queries.json")
    back = QueryLog.load(tmp_path / "queries.json")

    term = back.terms["жетонов"]
    assert term.accepted is True
    assert term.gloss == "đồng, the local currency"
    assert term.examples and term.examples[0][0] == "https://t.me/x/1"


def test_loading_a_log_that_cannot_be_understood_refuses(tmp_path):
    """Fail closed: an unreadable log is not an empty log, and an empty log is
    what the report calls "not one word could be mined"."""
    broken = tmp_path / "broken.json"
    broken.write_text("{not json at all", encoding="utf-8")
    with pytest.raises(QueryLogError):
        QueryLog.load(broken)

    wrong_schema = tmp_path / "wrong.json"
    wrong_schema.write_text('{"schema": "something/else", "rounds": []}', encoding="utf-8")
    with pytest.raises(QueryLogError):
        QueryLog.load(wrong_schema)

    with pytest.raises(FileNotFoundError):
        QueryLog.load(tmp_path / "nothing-here.json")


def test_save_creates_the_run_folder_if_it_is_missing(tmp_path):
    log = QueryLog()
    log.save(tmp_path / "run" / "notes" / "queries.json")
    assert (tmp_path / "run" / "notes" / "queries.json").exists()


# --------------------------------------------------------------------------
# The two stoppers declared in config were read by nothing
# --------------------------------------------------------------------------
def test_query_log_can_be_built_from_the_budgets_that_declare_it():
    import config

    budgets = config.Budgets(max_rounds=7, min_new_posts_per_round=11)
    log = QueryLog.from_budgets(budgets)
    assert log.max_rounds == 7
    assert log.min_new_posts == 11


# ==========================================================================
# The phrase check, the mining rules, and the two ways they used to fold
# ==========================================================================
def test_a_short_word_holds_its_place_in_the_phrase():
    """Words under `MIN_QUERY_TOKEN` were deleted from the query
    before the window slid, so what was checked was the query with its short
    words removed -- and the survivors only had to be adjacent to each other.

    Measured against exactly this corpus:

        'visa on arrival' -> True,  "found verbatim in retrieved text ..."   right
        'arrival on visa' -> True,  "found verbatim in retrieved text ..."   WRONG

    `arrival on visa` appears in no post; it was admitted because `arrival` and
    `visa` stand side by side in the second one. That is the recombination the
    ban exists to stop, arriving with the ban's own certificate.
    """
    log = QueryLog()
    log.record_posts([
        {"url": "a", "text": "you can get a visa on arrival at the airport"},
        {"url": "b", "text": "arrival visa is cheap"},
    ])

    ok, why = log.allows("visa on arrival")
    assert ok is True and "verbatim" in why

    ok, why = log.allows("arrival on visa")
    assert ok is False, why
    assert "refused as drift" in why
    assert "verbatim" not in why


def test_a_disjunction_means_the_same_thing_written_either_way_round():
    """The second half of the same defect. That deletion made an `OR` query
    order-dependent -- measured on a live 34-post news-channel corpus:

        'аренды OR жилья' -> True,  "found verbatim in retrieved text"
        'жилья OR аренды' -> False, "...never side by side in one post..."

    Same disjunction, opposite verdicts, decided by which side happened to come
    first in one post. `A OR B` is not a phrase; every side is a query that will
    really be run, so every side has to be derivable from the corpus.
    """
    log = QueryLog()
    log.record_posts([
        {"url": "a", "text": "снять квартиру: договор аренды жилья в центре"}])

    first = log.allows("аренды OR жилья")
    second = log.allows("жилья OR аренды")
    assert first[0] is True and second[0] is True
    assert first[0] == second[0]
    assert "each" in first[1] or "every side" in first[1]

    # And a side that was never retrieved refuses the whole query, by name.
    ok, why = log.allows("аренды OR взятка")
    assert ok is False
    assert "взятка" in why


def test_a_four_letter_corpus_word_does_not_license_every_longer_word():
    """`_phrase_match` accepted a pair on
    `len(token) >= MIN_STEM and word.startswith(token)` with `MIN_STEM = 4`, so
    any four-letter corpus word admitted every longer word beginning with those
    four letters -- and the reason read "found in retrieved text", which is the
    certificate the calling agent quotes when it says a query is corpus-derived.

    A run about furniture that retrieved «поставил стол у окна» was cleared to
    go and search «столица»; an English corpus saying "the band played" admitted
    "bandit".
    """
    ru = QueryLog()
    ru.record_posts([{"url": "u", "text": "поставил стол у окна"}])
    ok, why = ru.allows("столица")
    assert ok is False, why
    assert "refused as drift" in why
    assert ru.allows("стол")[0] is True          # the word itself still passes

    en = QueryLog()
    en.record_posts([{"url": "u", "text": "the band played"}])
    assert en.allows("bandit")[0] is False
    assert en.allows("band")[0] is True

    # And the tolerance the rule exists for is untouched.
    ru2 = QueryLog()
    ru2.record_posts([{"url": "u", "text": "сдал по рахмету инспектору"}])
    assert ru2.allows("рахмет")[0] is True
    ru3 = QueryLog()
    ru3.record_posts([{"url": "u", "text": "договор аренды подписан"}])
    assert ru3.allows("аренда")[0] is True

    # MAX_ENDING is the other half of the rule, and it does its own work: a
    # long shared stem is not enough when what is bolted onto it is a different
    # word. `report`/`reportedly` share six letters; `оплата`/`оплаченный`
    # share five. Neither is an inflection of the other.
    en2 = QueryLog()
    en2.record_posts([{"url": "u", "text": "the report was long"}])
    assert en2.allows("reportedly")[0] is False
    ru4 = QueryLog()
    ru4.record_posts([{"url": "u", "text": "оплата принята"}])
    assert ru4.allows("оплаченный")[0] is False


def test_the_stem_relation_is_stated_the_way_round_it_really_is():
    """The smaller defect on the same lines: the branch fired when
    the CORPUS word was the stem of the QUERY word, and the sentence said the
    opposite. The agent reads that sentence to judge the verdict."""
    log = QueryLog()
    log.record_posts([{"url": "u", "text": "сдал по рахмету инспектору"}])
    ok, why = log.allows("рахмет")
    assert ok is True
    # 'рахмет' is the stem; 'рахмету' is the corpus word it was found in.
    assert "'рахмет' as a stem of the corpus word 'рахмету'" in why

    other = QueryLog()
    other.record_posts([{"url": "u", "text": "новый арендатор въехал"}])
    ok, why = other.allows("арендатору")
    assert ok is True
    assert "inflection of the corpus word 'арендатор'" in why


@pytest.mark.parametrize("query", ["ква", "аре", "цен"])
def test_a_three_letter_query_is_refused_as_drift_not_by_the_short_word_floor(query):
    """`MIN_STEM` is what keeps the inflection tolerance from re-opening
    the fragment hole, and nothing pinned it: removing the length guard left the
    suite green while «ква» came back
    `True, "found in retrieved text: 'ква' as a stem of 'квартиры'"` -- the
    certificate, for a fragment. The existing test used «ар», two letters, which
    is refused by the OTHER floor (`MIN_QUERY_TOKEN`) and proves nothing about
    this one.
    """
    log = QueryLog()
    log.record_posts([{"url": "u", "text": "аренда квартиры в центре"}])
    ok, why = log.allows(query)
    assert ok is False, why
    assert "refused as drift" in why
    assert "floor" not in why, "this must be the stem rule, not the length floor"


def test_the_questions_own_words_are_excluded_in_every_inflection():
    """`candidates()` built `excluded` from the exact folded tokens
    of the seeds and the question, while the ban in the same class is
    stem-tolerant. Russian inflection mostly REPLACES the ending, so «аренда» in
    the exclusion list did not exclude «аренды», «аренде» or «арендой».

    Live run, question «Что пишут про аренду квартиры в Москве?», seed query
    «аренда квартиры», corpus 34 posts from a news channel: `queries record --top 20`
    returned «аренды» (freq 13, docs 12) in tenth place. «аренду» and «аренда»
    were both excluded; «аренды» was not. On a corpus dominated by one inflected
    form -- the normal case in Russian -- the top of the shortlist is the
    question restated, in a stage whose entire purpose is to find what the
    question could NOT have said.
    """
    log = QueryLog()
    posts = [{"url": f"u{i}",
              "text": "аренды растут, арендой недовольны, депозит требуют"}
             for i in range(4)]
    terms = [t.term for t in log.candidates(posts, exclude=["аренда квартиры"],
                                            top=10)]

    assert "аренды" not in terms
    assert "арендой" not in terms
    assert "депозит" in terms, "only the question's own word goes, not the batch"
    assert "аренды" in log.last_mining["excluded_as_the_question"]


def test_a_channel_footer_does_not_outrank_the_subjects_own_words():
    """A term was kept when `docs[term] >= min_documents` and then
    ranked by RAW FREQUENCY, so a line repeated in every post of a channel
    scored maximally on both counters.

    Measured on 34 live posts of a news channel with `--top 20`: five of the
    top twenty came from one footer, «🔗 Читать нас без VPN можно здесь:
    bit.ly/…», present in 17 of the 34 -- vpn (17 docs), youtube (13),
    рассылка (13), сайт (12) and the channel's own handle (11).
    The two terms genuinely specific to a cluster of posts, «лавры» and «упц»
    (freq 13, docs 2), sat below all five.
    """
    footer = "Читать нас без VPN можно здесь: newschannel рассылка сайт youtube"
    posts = [{"url": f"p{i}", "text": f"обычная новость номер {i}\n{footer}"}
             for i in range(17)]
    posts += [{"url": f"q{i}", "text": "упц лавры лавры упц монастырь " * 6}
              for i in range(2)]
    posts += [{"url": f"r{i}", "text": "другая заметка про погоду"}
              for i in range(15)]

    log = QueryLog()
    terms = [t.term for t in log.candidates(posts, top=20)]

    assert "упц" in terms and "лавры" in terms
    for furniture in ("vpn", "newschannel", "рассылка", "youtube"):
        assert furniture not in terms, f"{furniture} is the channel's furniture"
    assert terms[:2] == ["упц", "лавры"] or terms[:2] == ["лавры", "упц"]
    # And the removal is stated, never silent.
    assert log.last_mining["boilerplate_lines"]
    assert "furniture" in log.last_mining["note"]


def test_a_term_in_every_single_post_is_ranked_below_one_in_a_few():
    """The upper bound on document share, expressed as a penalty rather than a
    cliff: a term in 100 % of the documents is by construction either the
    language itself or boilerplate, and `min_documents` rewards it hardest."""
    posts = [{"url": f"p{i}", "text": f"новость про погоду номер {i}"}
             for i in range(20)]
    posts += [{"url": f"q{i}", "text": "рахмет рахмет рахмет"} for i in range(3)]
    ranked = [t.term for t in QueryLog().candidates(posts, top=10)]
    assert ranked[0] == "рахмет"
    assert ranked.index("рахмет") < ranked.index("новость")


def test_the_top_cut_is_reported_rather_than_silent():
    """`candidates()` stopped at `top` and returned nothing to say
    more existed, so the caller could not tell "25 candidates were all there
    was" from "25 of 400". The sibling module `discover.py` counts and names
    every drop; this one did not."""
    words = [f"слово{chr(1072 + i)}" for i in range(30)]
    posts = [{"url": f"u{i}", "text": " ".join(words)} for i in range(3)]
    log = QueryLog()

    few = log.candidates(posts, top=5)
    assert len(few) == 5
    assert log.last_mining["qualified"] == 30
    assert log.last_mining["cut_by_top"] == 25
    assert "--top 5 cut 25" in log.last_mining["note"]

    log2 = QueryLog()
    log2.candidates(posts, top=1000)
    assert log2.last_mining["cut_by_top"] == 0
    assert "cut" not in log2.last_mining["note"]


def test_a_batch_below_the_document_floor_says_so_instead_of_looking_empty():
    """`min_documents=2` means a batch of one post always returns
    `[]` -- and a rare query that found exactly one post is the normal shape of
    a first hit on real jargon. `"candidates": []` reads as "the corpus has no
    jargon" rather than "one document is below the floor"."""
    log = QueryLog()
    out = log.candidates([{"url": "u",
                           "text": "сдал экзамен по рахмету, инспектор попросил рахмет"}])
    assert out == []
    note = log.last_mining["note"]
    assert "1 post" in note
    assert "NOTHING here could have been mined" in note
    assert "min_documents=1" in note
    # And with the floor lowered the same batch does mine.
    assert [t.term for t in log.candidates(
        [{"url": "u2", "text": "сдал по рахмету"}], min_documents=1)] != []


def test_a_log_with_an_unknown_schema_is_refused():
    """The dangerous one: `allows()` keys the whole ban on
    `self.corpus_tokens`, so a log whose corpus does not survive the load leaves
    the drift ban SILENTLY DISABLED -- every query, however invented, comes back
    allowed with the sentence "no corpus retrieved yet", on a run whose
    `queries.json` on disk holds a full corpus. Removing the gate left the suite
    green."""
    log = QueryLog()
    log.record_posts([{"url": "u", "text": "аренда квартиры в центре"}])
    state = log.to_state()

    with pytest.raises(QueryLogError) as exc:
        QueryLog.from_state({**state, "schema": "telegram-research/querycraft/2"})
    assert "telegram-research/querycraft/2" in str(exc.value)

    with pytest.raises(QueryLogError):
        QueryLog.from_state({k: v for k, v in state.items() if k != "schema"})

    # The gate is the only thing between a wrong file and a disabled ban.
    reloaded = QueryLog.from_state(state)
    assert reloaded.allows("взятка")[0] is False


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "many"])
def test_a_number_that_is_not_finite_is_refused_by_name(bad):
    """`min_documents=nan` makes `docs[term] < nan` false for every term
    and silently removes the floor; `top=inf` silently removes the cut; and
    `int(inf)` raises OverflowError, which `from_state` did not catch and which
    leaves a `cmd_*` as a traceback and exit 1 -- the code reserved for a
    crash."""
    posts = [{"url": f"u{i}", "text": "аренда квартиры дорого"} for i in range(3)]
    with pytest.raises(QueryLogError) as exc:
        QueryLog().candidates(posts, top=bad)
    # The refusal names the damage. `int(nan)` and `int(inf)` raise on their own
    # too, so a message saying only "not a whole number" would leave NaN and a
    # typo indistinguishable -- and NaN is the one that silently removes a floor
    # rather than crashing.
    assert "top" in str(exc.value)
    if isinstance(bad, float):
        assert "finite" in str(exc.value)
    with pytest.raises(QueryLogError):
        QueryLog().candidates(posts, min_documents=bad)

    state = QueryLog().to_state()
    with pytest.raises(QueryLogError):
        QueryLog.from_state({**state, "max_rounds": bad})

    # `true` in a JSON config or on disk: `isinstance(True, int)` is True and
    # `int(True)` is 1, so without the shared check a boolean silently becomes
    # a ceiling of one.
    with pytest.raises(QueryLogError):
        QueryLog().candidates(posts, top=True)


def test_candidates_refuses_something_that_is_not_a_batch_of_posts():
    with pytest.raises(QueryLogError):
        QueryLog().candidates(42)
    assert QueryLog().candidates(None) == []


def test_top_zero_returns_nothing_and_says_it_did_no_work():
    """Zero means do nothing, and it never means unlimited. The cut sat
    AFTER the append (`out.append(...); if len(out) >= top: break`), so `top=0`
    returned one candidate."""
    posts = [{"url": f"u{i}", "text": "аренда квартиры дорого"} for i in range(3)]
    log = QueryLog()
    assert log.candidates(posts, top=0) == []
    assert "asks for no candidates" in log.last_mining["note"]
    assert "not a statement about the corpus" in log.last_mining["note"]
    assert log.candidates(posts, top=-5) == []


# --------------------------------------------------------------------------
# A torn query log switches the drift ban off, so the write is atomic
# --------------------------------------------------------------------------
def test_an_interrupted_save_leaves_the_log_that_was_already_there(tmp_path,
                                                                   monkeypatch):
    """`QueryLog.save` was a bare `write_text`, which truncates the destination
    before it writes a byte.

    `queries.json` is the only record of how many rounds a run has used AND the
    only copy of the corpus the drift ban is checked against, and `allows()`
    keys the ban on `self.corpus_tokens` -- so a log that comes back short does
    not fail loudly, it admits every query with the sentence "no corpus
    retrieved yet". Measured before the repair: a save that could not complete
    returned normally, and the log reloaded from disk held 0 posts and admitted
    «взятка» against a corpus that had refused it a moment earlier.

    Through `config.atomic_write_text` the same interruption raises, and the
    bytes on disk are the ones that were there before.
    """
    import config

    good = QueryLog()
    good.record_posts([{"url": "u", "text": "аренда квартиры в центре"}])
    path = tmp_path / "queries.json"
    good.save(path)
    assert QueryLog.load(path).allows("взятка")[0] is False

    def refuse(*args, **kwargs):
        raise PermissionError(5, "another process is holding it open")

    monkeypatch.setattr(config.os, "replace", refuse)
    with pytest.raises(config.AtomicWriteFailed):
        QueryLog().save(path)          # an empty log, over a good one

    back = QueryLog.load(path)
    assert len(back.corpus_text) == 1, "the log that was there must still be there"
    ok, why = back.allows("взятка")
    assert ok is False, f"the drift ban was switched off: {why}"


def test_a_save_leaves_no_temp_file_beside_the_log(tmp_path, monkeypatch):
    """A failed write must not leave an orphan in the run folder either -- the
    next command reads that directory."""
    import config

    log = QueryLog()
    log.record_posts([{"url": "u", "text": "аренда квартиры"}])
    path = tmp_path / "queries.json"
    log.save(path)
    assert [p.name for p in tmp_path.iterdir()] == ["queries.json"]

    def refuse(*args, **kwargs):
        raise PermissionError(5, "held open")

    monkeypatch.setattr(config.os, "replace", refuse)
    with pytest.raises(config.AtomicWriteFailed):
        log.save(path)
    assert [p.name for p in tmp_path.iterdir()] == ["queries.json"]


def test_the_saved_log_round_trips_exactly(tmp_path):
    """The format is unchanged by the way it is written: `run.write_queries`
    stages through this method and replaces the real file with those bytes."""
    log = QueryLog(max_rounds=4, min_new_posts=2)
    log.start_round(["аренда квартиры"])
    log.record_posts([{"url": "u1", "text": "аренда квартиры в центре"},
                      {"url": "u2", "text": "сдал по рахмету"}])
    path = log.save(tmp_path / "queries.json")

    assert json.loads(path.read_text(encoding="utf-8"))["schema"] == QueryLog.SCHEMA
    back = QueryLog.load(path)
    assert back.to_state() == log.to_state()
    assert back.allows("взятка")[0] is False
    assert back.allows("рахмет")[0] is True
