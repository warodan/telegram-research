"""Stage 3 -- building the queries, and mining the next ones out of the corpus.

The problem, stated with the example that produced it: searching for the Russian
word for *bribe* in a chat finds almost nothing, because in some communities
people write "по рахмету" instead -- a borrowed word for "thanks", so "сдал права
по рахмету" says the driving test was passed with one. Searching the word from
the question finds the smaller and worse half of what was said.

The mechanism has two halves and the second is the one that matters.

**Move 1 -- what a model can invent.** Synonyms, colloquialisms, transliteration,
Latin letters mixed into Cyrillic, misspellings, abbreviations, loanwords, the
local names of institutions. Cheap, worth doing, and it will never produce
"рахмет".

**Move 2 -- what the corpus knows.** Read a couple of hundred posts near the
subject and extract *what the people there call the thing*. The agent cannot
guess it; it can see it. This is why sources come before posts: the mechanism
has no input until some corpus exists to mine.

**Move 3 and onwards is a loop, not a third step.** Having found "рахмет", the
next round searches on it, and that yield carries the next layer -- neighbouring
euphemisms, the names of institutions, the words for the middlemen. Each layer
opens the next.

Three stoppers, written into the run's brief before it starts:

* a ceiling on rounds, set by depth level;
* a yield floor: a round that brings fewer than N new posts is the last one.
  A floor, not "nothing new" -- otherwise the loop lives forever on one lucky
  coincidence;
* a drift ban: a new query must be derivable from text already found. A word
  that appears in no retrieved post does not go into the next round, however
  plausible it sounds.

This module does the mechanical half: it keeps the ledger, enforces the three
stoppers, and produces a ranked shortlist of candidate jargon. Choosing which
candidates are real words for the subject is a judgement, and it stays with the
agent -- which is also why every candidate is handed over with the posts it came
from, so the judgement is made against evidence.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field, asdict
from pathlib import Path


class QueryLogError(RuntimeError):
    """A saved query log that cannot be read and understood. Never ignored."""


def _write_json(path: Path, text: str) -> None:
    """The one write in this module, and it is atomic.

    Separate from `save` so that the rule has a name and exactly one place to
    live: everything that puts a query log on disk goes through this line.
    """
    try:
        import config
    except ImportError as exc:          # pragma: no cover -- config is a sibling
        raise QueryLogError(
            "cannot write a query log without `config`: the atomic write lives "
            "there, and a bare write_text here silently disables the drift ban "
            "on an interrupted save"
        ) from exc
    config.atomic_write_text(path, text)


def _want_finite(value, name: str):
    """The shared NaN/Infinity check, as a number. Never a second copy."""
    try:
        import config
    except ImportError:                 # pragma: no cover -- config is a sibling
        return value
    try:
        return config.want_finite_number({name: value}, name)
    except ValueError as exc:
        raise QueryLogError(f"{name}: {exc}") from exc


def _want_whole(value, name: str) -> int:
    """A count from a caller, as a whole number, or `QueryLogError`.

    NaN and Infinity both arrive here -- out of a config override, and out of
    `json.loads`, which accepts them -- and both pass `isinstance(x, float)`.
    NaN makes every comparison false, so `min_documents=nan` silently removes
    the document floor and `top=inf` silently removes the cut; `int(inf)` then
    raises `OverflowError`, which no caller catches. The finite check is
    `config.want_finite_number` and is not copied here.
    """
    try:
        return int(_want_finite(value, name))
    except (TypeError, ValueError, OverflowError) as exc:
        raise QueryLogError(f"{name}={value!r} is not a whole number") from exc


# A line that repeats verbatim across a share of the batch is the channel's own
# furniture: a footer, a subscribe line, a disclaimer. Both floors matter -- the
# share, so one duplicated post cannot look like a footer, and the absolute
# count, so a two-post batch has no boilerplate at all.
BOILERPLATE_MIN_DOCS = 3
BOILERPLATE_SHARE = 0.25


def _repeat_floor(total: int) -> int:
    return max(BOILERPLATE_MIN_DOCS, math.ceil(total * BOILERPLATE_SHARE))


def _repeated_lines(folded_texts: list[str]) -> dict[str, int]:
    """Lines that stand, word for word, in enough of the batch to be furniture.

    Counted per DOCUMENT, not per occurrence, so a post that repeats its own
    footer twice still votes once. Returns `{}` rather than emptying the batch:
    if every line in every post is repeated -- a batch of identical posts --
    there is nothing here to separate furniture from content, and removing it
    all would turn a real corpus into an empty one.
    """
    total = len(folded_texts)
    if total < BOILERPLATE_MIN_DOCS:
        return {}
    counts: Counter = Counter()
    for text in folded_texts:
        lines = {ln.strip() for ln in text.splitlines() if ln.strip()}
        if len(lines) < 2:
            # Furniture is what is ATTACHED to content: a footer stands under a
            # post that also says something. A document that is nothing but one
            # line is that line, and six people posting the same sentence is a
            # community using the same words -- exactly what this stage exists
            # to find. Counting those as boilerplate deletes the finding.
            continue
        for line in lines:
            counts[line] += 1
    floor = _repeat_floor(total)
    repeated = {line: n for line, n in counts.items() if n >= floor}
    if not repeated:
        return {}
    survives = any(
        any(ln.strip() and ln.strip() not in repeated for ln in text.splitlines())
        for text in folded_texts
    )
    return repeated if survives else {}

# Deliberately small and multilingual. A long curated stop list is a place for a
# subject word to hide: better to under-filter and let ranking sort it out.
# The Russian half follows the Snowball/NLTK Russian stopword list, which is the
# one every Russian text pipeline uses; the English half is hand-picked.
STOPWORDS = {
    # ru
    "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а", "то",
    "все", "она", "так", "его", "но", "да", "ты", "к", "у", "же", "вы", "за",
    "бы", "по", "только", "ее", "мне", "было", "вот", "от", "меня", "еще",
    "нет", "о", "из", "ему", "теперь", "когда", "даже", "ну", "вдруг", "ли",
    "если", "уже", "или", "ни", "быть", "был", "него", "до", "вас", "нибудь",
    "опять", "уж", "вам", "ведь", "там", "потом", "себя", "ничего", "ей",
    "может", "они", "тут", "где", "есть", "надо", "ней", "для", "мы", "тебя",
    "их", "чем", "была", "сам", "чтоб", "без", "будто", "чего", "раз", "тоже",
    "себе", "под", "будет", "ж", "тогда", "кто", "этот", "того", "потому",
    "этого", "какой", "совсем", "ним", "здесь", "этом", "один", "почти",
    "мой", "тем", "чтобы", "нее", "были", "куда", "зачем", "всех", "никогда",
    "можно", "при", "наконец", "два", "об", "другой", "хоть", "после", "над",
    "больше", "тот", "через", "эти", "нас", "про", "всего", "них", "какая",
    "много", "разве", "три", "эту", "моя", "впрочем", "хорошо", "свою",
    "этой", "перед", "иногда", "лучше", "чуть", "том", "нельзя", "такой",
    "им", "более", "всегда", "конечно", "всю", "между",
    # en
    "the", "a", "an", "and", "or", "but", "if", "then", "than", "that", "this",
    "these", "those", "is", "are", "was", "were", "be", "been", "being", "to",
    "of", "in", "on", "at", "by", "for", "with", "about", "as", "from", "it",
    "its", "you", "your", "we", "our", "they", "their", "he", "she", "his",
    "her", "not", "no", "yes", "can", "could", "will", "would", "should",
    "have", "has", "had", "do", "does", "did", "just", "so", "up", "out",
    "все", "http", "https", "com", "www", "t", "me",
}

TOKEN_RE = re.compile(r"[^\W\d_]{3,}", re.UNICODE)

# The same alphabet without the length floor, so that a query made of words too
# short to be leads can be refused BY NAME rather than by silently matching
# nothing. `allows` used to be a naked substring test over the corpus: the
# single letter `о` was admitted, and so was `да кварт` -- a fragment spanning
# the word boundary inside "аренда квартиры".
QUERY_TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

MIN_QUERY_TOKEN = 3      # matches TOKEN_RE: shorter than this is not a lead

# The inflection tolerance, and the two numbers that keep it from becoming a
# prefix match. `MIN_STEM` was 4 and the rule was a bare `startswith`, so any
# four-letter corpus word admitted every longer word beginning with those four
# letters: a run about furniture that retrieved «поставил стол у окна» was
# cleared to go and search «столица», and an English corpus saying "the band
# played" admitted "bandit" -- both with the sentence "found in retrieved text",
# which is the certificate the calling agent quotes when it says a query is
# corpus-derived rather than invented.
#
# Two forms of one word share a long stem and differ only in a short ending, so
# that is what is required: at least `MIN_STEM` letters in common, and neither
# tail longer than `MAX_ENDING`. It keeps рахмет/рахмету and аренда/аренды, and
# drops стол/столица (4 letters in common) and band/bandit (4). It still admits
# аренда/арендатор, which share six letters and are the same root -- a lead
# worth following, and one the corpus really did say.
MIN_STEM = 5
MAX_ENDING = 3
# What the two words are allowed to differ BY. `MIN_STEM` and `MAX_ENDING` bound
# the shape of the difference and cannot bound its content: «аренда»/«арендой»
# and «столик»/«столица» have exactly the same shape -- five shared letters,
# then one letter against two -- and one pair is a word in two forms while the
# other is two words. No rule about lengths can separate those, so this one is
# about the letters: the tail each word is left with has to be an ENDING.
#
# Cyrillic first, because that is the language the drift ban was written for and
# these are inflectional endings rather than derivational suffixes -- «-ка»,
# «-ик», «-ица», «-тель» make a different word and are deliberately absent. Then
# the English inflections, because a corpus of English posts is an ordinary case
# here. `ё` is folded to `е` before this is consulted, so the `ё` forms are the
# same strings. The bare consonants in the last Cyrillic row are not endings on
# their own: prefix matching eats the vowel, so «аренда»/«арендами» is left
# holding «ми» and «квартира»/«квартирах» «х». They are what is left of «ами»
# and «ах» after the shared stem has taken the rest.
INFLECTIONAL_ENDINGS = frozenset("""
    а я о е и ы у ю ь й
    ой ей ом ем ах ях ам ям ов ев ью ия ии ие ий ый ая яя ое ее ые их ых ым им
    ую юю ей ет ит ут ют ат ят ла ло ли ть ти ся сь ем им те ешь ишь
    ого его ому ему ами ями ыми ими ете ите
    м х в го му ми
    s es ed ing d ies
""".split()) | {""}

# The one query operator this skill's own examples use: a bare, upper-case OR
# between two alternatives (`аренда OR квартиры`). It is detected on the query
# as WRITTEN, so an ordinary lower-case "or" inside an English phrase stays an
# ordinary word.
_OR_SPLIT_RE = re.compile(r"\s+OR\s+")


@dataclass
class JargonTerm:
    """A word the corpus uses, with everything needed to judge and to cite it."""

    term: str
    round_found: int
    frequency: int
    documents: int
    examples: list = field(default_factory=list)   # (url, snippet) pairs
    gloss: str | None = None                       # what it means -- the agent fills this
    accepted: bool = False
    # What the shortlist was ordered by, carried so the order can be argued
    # with. Raw frequency put a channel's footer above the subject's own words;
    # see `candidates`. Optional in a stored log written before it existed.
    score: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Round:
    number: int
    queries: list = field(default_factory=list)
    new_posts: int = 0
    new_terms: list = field(default_factory=list)
    stopped_because: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


class QueryLog:
    """The ledger of what was searched, what it found, and where words came from.

    It outlives the run. A second piece of research on the same subject starts
    with the vocabulary the first one paid for, which is the entire reason the
    round number is recorded against every term.
    """

    def __init__(self, max_rounds: int = 3, min_new_posts: int = 3):
        # The same check `from_state` runs, and for the same reason: both
        # numbers arrive from a config override as well as from disk, and NaN
        # passes `isinstance(x, float)` while making every comparison false --
        # `max_rounds=nan` removed the round ceiling in silence. A constructor
        # that accepted what the restore path refuses was a hole with a door
        # next to it.
        self.max_rounds = _want_whole(max_rounds, "max_rounds")
        self.min_new_posts = _want_whole(min_new_posts, "min_new_posts")
        self.rounds: list[Round] = []
        self.terms: dict[str, JargonTerm] = {}
        self.seen_post_urls: set[str] = set()
        self.corpus_text: list[str] = []
        self.corpus_tokens: set[str] = set()
        self._absorbed: set[str] = set()   # folded chunks already in the corpus
        # One token sequence per retrieved post, in the order the post said them.
        # The drift ban is a PHRASE ban, so it needs to know which words stood
        # next to which; a flat set of tokens cannot tell a phrase from a
        # recombination of two different posts. Derived from `corpus_text`, so it
        # needs no place in the saved schema.
        self._token_seqs: list[list[str]] = []
        # What the last `candidates()` call did, in numbers. A shortlist that is
        # a cut of a longer list, a floor that nothing could clear, a footer
        # removed -- each of them makes `candidates: []` or a short list mean
        # something quite different, and none of them was said out loud.
        self.last_mining: dict = {}

    # -- the three stoppers ------------------------------------------------
    def may_continue(self) -> tuple[bool, str]:
        """Ask before starting another round. Returns (may, why not)."""
        if len(self.rounds) >= self.max_rounds:
            return False, f"round ceiling of {self.max_rounds} reached"
        if self.rounds:
            last = self.rounds[-1]
            if last.new_posts < self.min_new_posts:
                return False, (
                    f"round {last.number} brought {last.new_posts} new posts, below "
                    f"the floor of {self.min_new_posts}"
                )
        return True, ""

    def allows(self, query: str) -> tuple[bool, str]:
        """The drift ban: a query must be derivable from text already retrieved.

        Checked against the actual corpus, not against a memory of it. A word an
        agent finds plausible but that appears in none of the posts we hold is
        not a lead, it is an invention, and following it is how a run about
        driving-test bribery ends up reading about something else entirely.

        Three ways this used to be off when it mattered.

        * It keyed on the round ledger -- `if not self.rounds: return True` --
          so the natural order of work (pick the queries, check them, *then*
          `start_round`) put every batch in the state where the ban was off,
          corpus or no corpus. It keys on the corpus now, which is what the ban
          is actually about.
        * It was a naked substring test, so `о` and `да кварт` were admitted
          against a corpus containing "аренда квартиры".
        * Then it became a word-by-word test, which is the bug this docstring is
          mostly about. **The ban is a PHRASE ban.** The contract the calling
          agent reads says a query must appear *verbatim* in text already
          retrieved; matching each word separately admits a query assembled out
          of two different posts and hands back the sentence "found verbatim in
          retrieved text" about a phrase that occurs nowhere. Measured: a corpus
          of "риелтор просит депозит за студию" and "сдал по рахмету" admitted
          `рахмету студию` -- exactly the drift the ban exists to stop, arriving
          with a certificate saying it is not drift.

        So the query's words must stand next to each other, in this order, inside
        ONE retrieved post. One thing is deliberately not strict: inflection --
        the corpus says «рахмету» and the query worth running is «рахмет», so a
        word may match a corpus word it shares a stem with (`MIN_STEM`,
        `MAX_ENDING`).

        **A short word is not deleted from the phrase.** It used to be: words
        under `MIN_QUERY_TOKEN` were dropped before the window slid, so what was
        checked was the query with its short words removed and the survivors only
        had to be adjacent to each other -- and the verdict came back as "found
        verbatim in retrieved text" about a string that occurs in no post at all.
        Measured on a corpus of "you can get a visa on arrival at the airport"
        and "arrival visa is cheap": `arrival on visa` was admitted, verbatim, because
        `arrival` and `visa` stand side by side in the second post. The same
        deletion made a disjunction depend on operand order -- `аренды OR жилья`
        admitted and `жилья OR аренды` refused against one corpus.

        The corpus keeps its short words now (`_token_seqs` is built with
        `QUERY_TOKEN_RE`), so `visa on arrival` matches position for position and
        the word "verbatim" is literally true when it is used. `MIN_QUERY_TOKEN`
        stays as a floor on the QUERY: a query with no word of three letters in
        it has nothing in it to check.

        `A OR B` is a disjunction, not a phrase, and is checked as one: every
        side must be derivable from the corpus, which is both stricter than the
        old accident and free of its dependence on which side was written first.
        """
        if not _fold(query):
            return False, "empty query"
        if not self._token_seqs:
            # Keyed on the sequences the check below really slides its window
            # over, not on `corpus_tokens`. Those are two different tokenisers:
            # `corpus_tokens` keeps only words of three letters or more, so a
            # corpus of Chinese, of Japanese, or of nothing but short words
            # filled `_token_seqs` and left `corpus_tokens` empty -- and the ban
            # switched itself off, admitting every invented query with the
            # sentence "no corpus retrieved yet" about a corpus that was there.
            return True, "no corpus retrieved yet — the question itself is the seed"
        branches = [b for b in _OR_SPLIT_RE.split(str(query).strip())]
        if len(branches) > 1:
            said: list[str] = []
            for branch in branches:
                ok, why = self._allows_phrase(branch)
                if not ok:
                    return False, (
                        f"the {branch.strip()!r} side of this OR is not derivable "
                        f"from any retrieved post — {why}. Every side of a "
                        "disjunction is a query that will really be run"
                    )
                said.append(f"{branch.strip()!r}: {why}")
            return True, "every side of the OR was found in retrieved text — " + \
                "; ".join(said)
        return self._allows_phrase(query)

    def _allows_phrase(self, query: str) -> tuple[bool, str]:
        """One phrase, checked against the corpus. `allows` is the public door."""
        needle = _fold(query)
        if not needle:
            return False, "empty query"
        spoken = QUERY_TOKEN_RE.findall(needle)
        if not any(len(w) >= MIN_QUERY_TOKEN for w in spoken):
            return False, (
                f"{query!r} holds no word of {MIN_QUERY_TOKEN} letters or more — "
                "every word in it is below that floor, and a fragment that short "
                "matches almost any corpus. There is nothing here to check"
            )
        stems = self._phrase_in_corpus(spoken)
        if stems is not None:
            named = [s for s in stems if s]
            if named:
                return True, "found in retrieved text as a phrase, " + \
                    "; ".join(named)
            return True, "found verbatim in retrieved text"
        if len(spoken) > 1:
            return False, (
                f"{query!r} appears in no post retrieved so far — its words may "
                "each appear, but never side by side in one post, so it was "
                "assembled rather than read: refused as drift, not as a bad idea"
            )
        return False, (
            f"{query!r} appears in no post retrieved so far ({spoken[0]!r} is a word "
            "of none of them) — refused as drift, not as a bad idea"
        )

    def _phrase_in_corpus(self, words: list[str]) -> list[str] | None:
        """Do these words stand together, in order, in one retrieved post?

        Returns one entry per query word -- `""` where it matched a corpus word
        outright, a sentence where it matched as a form of one -- or `None` for
        the whole call when no post carries the phrase. `MIN_STEM` is what keeps
        the inflection tolerance from re-opening the fragment hole.
        """
        width = len(words)
        for tokens in self._token_seqs:
            for start in range(len(tokens) - width + 1):
                stems = _phrase_match(words, tokens[start:start + width])
                if stems is not None:
                    return stems
        return None

    # -- recording ---------------------------------------------------------
    def start_round(self, queries) -> Round:
        rnd = Round(number=len(self.rounds) + 1, queries=list(queries))
        self.rounds.append(rnd)
        return rnd

    def absorb(self, messages) -> int:
        """Take the text of retrieved posts into the corpus. Counts nothing.

        The corpus is *what we have retrieved*, and the drift ban is checked
        against it, so text that was read has to enter it whether or not the
        post carrying it had a URL to be counted by. Without this, `candidates()`
        mined terms out of a batch and `allows()` then refused them as drift --
        the mining stage arguing with the ban that exists to serve it.
        """
        added = 0
        for msg in messages:
            text = getattr(msg, "text", None) or (msg.get("text") if isinstance(msg, dict) else "")
            folded = _fold(text)
            if not folded or folded in self._absorbed:
                continue
            self._absorbed.add(folded)
            self.corpus_text.append(folded)
            # EVERY word, short ones included -- `QUERY_TOKEN_RE`, not
            # `TOKEN_RE`. The phrase ban slides a window over this sequence, so
            # a sequence with the short words stripped out of it is a sequence in
            # which words that were three words apart in the post stand side by
            # side. `corpus_tokens` keeps the three-letter floor: that set is
            # what the miner ranks, and a two-letter word is not a lead.
            self._token_seqs.append(QUERY_TOKEN_RE.findall(folded))
            self.corpus_tokens.update(TOKEN_RE.findall(folded))
            added += 1
        return added

    def record_posts(self, messages) -> int:
        """Add posts to the corpus; return how many of them were new."""
        messages = list(messages)
        self.absorb(messages)
        fresh = 0
        for msg in messages:
            url = getattr(msg, "url", None) or (msg.get("url") if isinstance(msg, dict) else None)
            if not url or url in self.seen_post_urls:
                continue
            self.seen_post_urls.add(url)
            fresh += 1
        if self.rounds:
            self.rounds[-1].new_posts += fresh
        return fresh

    # -- mining ------------------------------------------------------------
    def candidates(self, messages, *, exclude=(), top: int = 25,
                   min_documents: int = 2) -> list[JargonTerm]:
        """Rank what the corpus says often and the question never said.

        Frequency alone would return the language's most ordinary words, so a
        term must also appear in at least `min_documents` separate posts: a word
        one person used twice in one message is that person's, and a word six
        people used is the community's.

        This produces a shortlist, never a decision. Each candidate carries the
        posts it came from so that whoever accepts it can read them.

        The batch enters the corpus on the way in. Anything mined here was
        therefore read here, so `allows()` admits it: a term this method returns
        can never be refused as drift by the ban in the same object.

        Three things this used to get wrong on a real channel, all measured on
        34 posts of one large news channel.

        **The exclusion was exact and the ban was not.** `excluded` held the
        folded tokens of the question and the seed queries, matched literally,
        while everything else in this class matches by stem. Russian inflection
        mostly replaces the ending, so «аренда» in the exclusion list did not
        exclude «аренды» -- and the stage whose entire purpose is to find what
        the question could NOT have said returned the question restated, tenth
        on the shortlist. It excludes by `same_word` now.

        **A channel's footer scored maximally on both counters.** «Читать нас
        без VPN можно здесь…» sat in 17 of the 34 posts, so `vpn`, `youtube`,
        `рассылка`, `сайт` and the channel's own handle took five of the top
        twenty, above the two words that were genuinely specific to a cluster of
        the posts. A line repeated verbatim
        across a quarter of the batch is furniture, and it is removed before
        anything is counted; what was removed is recorded, never silent.

        **Ranking was raw frequency with only a floor on document count.** A
        term in every document is by construction either boilerplate or the
        language itself, and `min_documents` rewards it hardest. The order is
        `frequency * (documents + 1 - share)` -- literally
        `frequency * (total - documents + 1) / total` -- which leaves a term in
        a handful of posts ahead of one in all of them and still lets a single
        very frequent word win. The number is carried on the term as `score`, so
        the order can be argued with rather than trusted.

        Everything that shortened the answer is in `self.last_mining`: the
        `top` cut, the `min_documents` floor, the exclusions, the footer. A
        batch of one post can never clear a floor of two, and `[]` from it used
        to read as "this corpus has no jargon".
        """
        try:
            messages = list(messages or [])
        except TypeError as exc:        # never a bare TypeError to a caller
            raise QueryLogError(
                f"`candidates` takes a sequence of posts, not a "
                f"{type(messages).__name__}"
            ) from exc
        top = _want_whole(top, "top")
        min_documents = _want_whole(min_documents, "min_documents")
        self.absorb(messages)
        self.last_mining = {
            "documents": len(messages), "min_documents": min_documents,
            "top": top, "qualified": 0, "returned": 0, "cut_by_top": 0,
            "below_min_documents": 0, "already_accepted": 0,
            "excluded_as_the_question": [], "boilerplate_lines": [], "note": "",
        }
        if top < 1:
            # `if len(out) >= top: break` sits AFTER the append, so `top=0`
            # returned one candidate -- zero work reported as a measurement.
            self.last_mining["note"] = (
                f"--top {top} asks for no candidates, so none were returned. "
                "That is not a statement about the corpus."
            )
            return []
        # Every exclusion is excluded BY ITS WORDS as well as whole. The callers
        # pass whole query strings -- the brief's seed queries and every query
        # already run -- and folding those as single strings meant a multi-word
        # query never matched any single token: `exclude=['аренда квартиры']`
        # returned `['аренда', 'квартиры', ...]` at the top of the shortlist,
        # in a stage whose whole purpose is to find what the question could NOT
        # have said, and a round spent re-searching them is a round off the
        # ceiling.
        excluded = set(STOPWORDS)
        asked: set[str] = set()          # the question's and the seeds' own words
        if isinstance(exclude, str):
            # One phrase, not a sequence of letters. A string is iterable, so
            # `exclude="аренда"` excluded six single characters -- none of them
            # a token this method counts -- and the question's own word came
            # back at the top of the shortlist with nothing said about it.
            exclude = [exclude]
        for phrase in exclude or ():
            folded = _fold(phrase)
            if not folded:
                continue
            excluded.add(folded)
            for word in TOKEN_RE.findall(folded):
                excluded.add(word)
                asked.add(word)

        texts = [
            (getattr(msg, "text", None)
             or (msg.get("text") if isinstance(msg, dict) else "") or "")
            for msg in messages
        ]
        urls = [
            (getattr(msg, "url", None)
             or (msg.get("url") if isinstance(msg, dict) else "") or "")
            for msg in messages
        ]
        furniture = _repeated_lines([_fold(t) for t in texts])
        self.last_mining["boilerplate_lines"] = [
            {"line": line[:120], "documents": n} for line, n in furniture.items()
        ]

        freq: Counter = Counter()
        docs: Counter = Counter()
        examples: dict[str, list] = {}
        dropped_as_asked: set[str] = set()

        for text, url in zip(texts, urls):
            # Fold first, then tokenise -- the same order `absorb` uses, so a
            # term mined here is always literally a corpus token and the ban in
            # this same object can never refuse it.
            body = "\n".join(
                line for line in _fold(text).splitlines()
                if line.strip() not in furniture
            )
            tokens = []
            for token in TOKEN_RE.findall(body):
                if not token or len(token) < 3:
                    continue
                if token in excluded:
                    continue
                # Stem-tolerant, the way the ban is: «аренды» IS the question's
                # «аренда» in another case, and this stage exists to return what
                # the question could not have said.
                if any(same_word(token, word) for word in asked):
                    dropped_as_asked.add(token)
                    continue
                tokens.append(token)
            for token in tokens:
                freq[token] += 1
            for token in set(tokens):
                docs[token] += 1
                if len(examples.setdefault(token, [])) < 3:
                    examples[token].append((url, _snippet(text, token)))

        self.last_mining["excluded_as_the_question"] = sorted(dropped_as_asked)

        total = len(messages) or 1
        qualified: list[JargonTerm] = []
        below_floor = 0
        already = 0
        for term, count in freq.items():
            if docs[term] < min_documents:
                below_floor += 1
                continue
            if term in self.terms:
                already += 1
                continue
            qualified.append(
                JargonTerm(
                    term=term,
                    round_found=len(self.rounds),
                    frequency=count,
                    documents=docs[term],
                    examples=examples.get(term, []),
                    score=round(count * (total - docs[term] + 1) / total, 3),
                )
            )
        qualified.sort(key=lambda t: (-t.score, -t.frequency, t.term))
        out = qualified[:top]

        self.last_mining.update({
            "qualified": len(qualified), "returned": len(out),
            "cut_by_top": len(qualified) - len(out),
            "below_min_documents": below_floor, "already_accepted": already,
        })
        notes = []
        if len(qualified) > len(out):
            notes.append(
                f"{len(qualified)} terms qualified and {len(out)} are shown: "
                f"--top {top} cut {len(qualified) - len(out)} of them"
            )
        if len(messages) < min_documents:
            # A rare query that found exactly one post is the normal shape of a
            # first hit on real jargon, and `[]` from it reads as "the corpus
            # has no jargon" rather than "one document cannot clear a floor of
            # two".
            notes.append(
                f"this batch holds {len(messages)} post(s) and a term must appear "
                f"in {min_documents} separate ones to be offered, so NOTHING here "
                "could have been mined whatever the posts said. Read more before "
                "concluding the corpus has no vocabulary, or pass "
                "min_documents=1 and judge the words yourself"
            )
        elif not out and below_floor:
            notes.append(
                f"{below_floor} terms were seen but none of them in "
                f"{min_documents} separate posts"
            )
        if furniture:
            notes.append(
                f"{len(furniture)} line(s) repeated across at least "
                f"{_repeat_floor(total)} of the {total} posts were treated as the "
                "channel's own furniture and not counted"
            )
        self.last_mining["note"] = ". ".join(notes)
        return out

    def accept(self, term: JargonTerm, gloss: str | None = None) -> JargonTerm:
        """Take a candidate into the vocabulary. The agent decides; this records."""
        term.gloss = gloss
        term.accepted = True
        self.terms[term.term] = term
        if self.rounds:
            self.rounds[-1].new_terms.append(term.term)
        return term

    # -- persistence -------------------------------------------------------
    #
    # A QueryLog that only ever lived in one process could not be reported on:
    # `tg.py report` had no way to know what stage 3 had done, so it printed
    # "not one word could be mined" over a run that had mined four. The whole
    # object round-trips, corpus included, because the drift ban is checked
    # against the corpus and a log reloaded without it would silently admit
    # anything.
    SCHEMA = "telegram-research/querycraft/1"

    def to_state(self) -> dict:
        """Everything needed to rebuild this object exactly."""
        return {
            "schema": self.SCHEMA,
            "max_rounds": self.max_rounds,
            "min_new_posts": self.min_new_posts,
            "rounds": [r.as_dict() for r in self.rounds],
            "terms": [t.as_dict() for t in self.terms.values()],
            "seen_post_urls": sorted(self.seen_post_urls),
            "corpus_text": list(self.corpus_text),
            "corpus_tokens": sorted(self.corpus_tokens),
        }

    @classmethod
    def from_state(cls, state: dict) -> "QueryLog":
        if not isinstance(state, dict):
            raise QueryLogError(f"a query log must be a JSON object, got {type(state).__name__}")
        schema = state.get("schema")
        if schema != cls.SCHEMA:
            raise QueryLogError(
                f"unknown query-log schema {schema!r}; this build writes {cls.SCHEMA!r}. "
                "Refusing to guess what the file means"
            )
        # Every number below comes off disk, where `json.loads` will hand
        # back NaN and Infinity without a word. `_want_whole` is the shared
        # finite check; `OverflowError` is in the except clause because
        # `int(inf)` raises it and it used to leave `from_state` as a traceback.
        try:
            log = cls(max_rounds=_want_whole(state["max_rounds"], "max_rounds"),
                      min_new_posts=_want_whole(state["min_new_posts"],
                                                "min_new_posts"))
            for raw in state.get("rounds", []):
                log.rounds.append(Round(
                    number=_want_whole(raw["number"], "round number"),
                    queries=list(raw.get("queries", [])),
                    new_posts=_want_whole(raw.get("new_posts", 0), "new_posts"),
                    new_terms=list(raw.get("new_terms", [])),
                    stopped_because=raw.get("stopped_because"),
                ))
            for raw in state.get("terms", []):
                term = JargonTerm(
                    term=raw["term"],
                    round_found=_want_whole(raw.get("round_found", 0), "round_found"),
                    frequency=_want_whole(raw.get("frequency", 0), "frequency"),
                    documents=_want_whole(raw.get("documents", 0), "documents"),
                    # JSON has no tuple; examples are (url, snippet) pairs and
                    # every reader of them indexes by position.
                    examples=[tuple(e) for e in raw.get("examples", [])],
                    gloss=raw.get("gloss"),
                    accepted=bool(raw.get("accepted", False)),
                    # Absent from a log written before ranking carried its own
                    # number; a stored 0.0 simply means "not ranked here".
                    score=float(_want_finite(raw.get("score", 0) or 0, "score")),
                )
                log.terms[term.term] = term
            log.seen_post_urls = set(state.get("seen_post_urls", []))
            # Re-folded on the way in, not trusted as stored: a log written
            # before ё and е were folded together carries both spellings, and
            # the ban would go on calling one of them drift for the life of the
            # file. Folding is idempotent, so a current file is unchanged by it.
            log.corpus_text = [_fold(t) for t in state.get("corpus_text", [])]
            log._absorbed = set(log.corpus_text)
            log._token_seqs = [QUERY_TOKEN_RE.findall(t) for t in log.corpus_text]
            log.corpus_tokens = {_fold(t) for t in state.get("corpus_tokens", [])}
            log.corpus_tokens.discard("")
        except (KeyError, TypeError, ValueError) as exc:
            raise QueryLogError(f"malformed query log: {exc}") from exc
        return log

    def save(self, path) -> Path:
        """Write the whole log to `path` as JSON. Returns the path written.

        Through `config.atomic_write_text` -- a private temp file and
        `os.replace` -- and never a bare `write_text`, which truncates the
        destination before it writes a byte.

        This one matters more than a file write usually does. `queries.json` is
        the only record of how many rounds a run has used AND the only copy of
        the corpus the drift ban is checked against, and `allows()` keys the ban
        on `self.corpus_tokens`: a log that comes back short admits every query,
        however invented, with the sentence "no corpus retrieved yet -- the
        question itself is the seed". Measured on this file before the repair: a
        save that could not complete returned normally, and the log reloaded
        from disk held 0 posts and admitted «взятка» against a corpus that had
        refused it a moment earlier. A torn write here does not break the ban
        loudly; it switches it off quietly, which is the failure this whole
        module exists to prevent.

        With the atomic write the same interruption raises
        `config.AtomicWriteFailed` and the bytes on disk are the ones that were
        there before. The GUARD is deliberately not taken here: it belongs to
        whoever owns the destination -- `run.write_queries` takes it around
        `queries.json` -- and a serialiser cannot know the caller is not already
        holding it.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(path, json.dumps(self.to_state(), ensure_ascii=False, indent=2))
        return path

    @classmethod
    def load(cls, path) -> "QueryLog":
        """Read a log back. Missing file raises FileNotFoundError; a file that
        cannot be understood raises QueryLogError -- never a silent empty log,
        because an empty log is what the report calls "nothing was mined"."""
        path = Path(path)
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise QueryLogError(f"cannot read the query log at {path}: {exc}") from exc
        return cls.from_state(state)

    # -- output ------------------------------------------------------------
    def as_dict(self) -> dict:
        return {
            "max_rounds": self.max_rounds,
            "min_new_posts": self.min_new_posts,
            "rounds": [r.as_dict() for r in self.rounds],
            "vocabulary": [t.as_dict() for t in self.terms.values()],
            "posts_seen": len(self.seen_post_urls),
            # What the last mining actually did. A shortlist is a cut of a
            # longer list far more often than it looks, and `[]` has three
            # different meanings; this is where they are told apart.
            "last_mining": self.last_mining or None,
        }

    def to_markdown(self) -> str:
        """`queries.md` -- every query by round, and what each one found."""
        lines = ["# Queries", ""]
        for rnd in self.rounds:
            lines.append(f"## Round {rnd.number}")
            lines.append("")
            lines.append(f"- new posts: {rnd.new_posts}")
            if rnd.stopped_because:
                lines.append(f"- stopped: {rnd.stopped_because}")
            lines.append("")
            lines.append("| query | derived from |")
            lines.append("| --- | --- |")
            for q in rnd.queries:
                origin = "the question" if rnd.number == 1 else "the corpus"
                lines.append(f"| `{q}` | {origin} |")
            lines.append("")
        if self.terms:
            lines += ["## Vocabulary mined from the corpus", "",
                      "| term | round | posts | gloss | example |",
                      "| --- | --- | --- | --- | --- |"]
            for term in self.terms.values():
                example = term.examples[0][0] if term.examples else ""
                lines.append(
                    f"| `{term.term}` | {term.round_found} | {term.documents} | "
                    f"{term.gloss or ''} | {example} |"
                )
            lines.append("")
        return "\n".join(lines)


# Cyrillic ё and е are separate code points and NFKC does not merge them, but
# Russian writes the same word both ways: "ещё" and "еще" are one word, and a
# ban that calls the second one drift is calling a spelling variant an
# invention. The corpus and the query go through the same replacement, so it cannot
# matter which spelling either of them used.
#
# Deliberately NOT folded here: Latin letters standing in for Cyrillic ones
# (`арeнда`) and transliteration (`arenda`). Those are not spellings of the same
# characters, they are different strings that mean the same thing, and turning
# them into one is a transliteration feature rather than a folding rule.
_YO = str.maketrans({"ё": "е", "Ё": "е"})


def same_word(a: str, b: str) -> bool:
    """Are these two folded words the same word in two forms?

    Not a stemmer and not trying to be. Two forms of a word share a long stem
    and differ only in a short ending, so that is the whole test: at least
    `MIN_STEM` letters in common from the start, and neither tail longer than
    `MAX_ENDING`.

    The rule it replaces was `len(x) >= 4 and y.startswith(x)`, which admitted
    any longer word beginning with any four-letter corpus word -- стол/столица,
    band/bandit -- and it is also why the exclusion list in `candidates` could
    not see that «аренды» is the question's own «аренда». Russian inflection
    mostly REPLACES the ending rather than adding to it, so a prefix test cannot
    match two inflected forms of one word to each other at all.

    Public because two callers need exactly one answer to this question: the
    drift ban, deciding whether a query word was really said, and the miner,
    deciding whether a candidate is the question restated.
    """
    if a == b:
        return bool(a)
    if not a or not b:
        return False
    limit = min(len(a), len(b))
    common = 0
    while common < limit and a[common] == b[common]:
        common += 1
    if common < MIN_STEM:
        return False
    tails = (a[common:], b[common:])
    if max(len(t) for t in tails) > MAX_ENDING:
        return False
    # And what is left over has to be an ending, not the rest of another word.
    # Length alone handed the drift ban a certificate for «квартира»/«квартал»
    # (five shared letters, then «ира» against «ал»), «столик»/«столица» and
    # «визит»/«визитка»: three pairs of DIFFERENT words, admitted with the
    # sentence "as a form of" -- the line the calling agent quotes when it says
    # a query came out of the corpus rather than out of the model. The pairs the
    # tolerance exists for differ by real endings and are untouched:
    # «аренда»/«аренды», «аренда»/«арендой», «рахмет»/«рахмету»,
    # «арендатор»/«арендатору».
    return all(t in INFLECTIONAL_ENDINGS for t in tails)


def _phrase_match(words: list[str], window: list[str]) -> list[str] | None:
    """Align a query's words with an equal-length run of one post's words.

    Returns one entry per word -- empty where the two are the same word, a
    sentence where they are two forms of one word -- or `None` if any pair does
    not match at all. Every position must answer, short words included: a word
    the query said is a word the surface will be asked for.
    """
    stems: list[str] = []
    for word, token in zip(words, window):
        if word == token:
            stems.append("")
            continue
        if same_word(word, token):
            # Which of the two is the stem is stated the way round it really is.
            # The old sentence read "'столица' as a stem of 'стол'" -- the
            # relation backwards, in the one sentence the agent reads to judge
            # the verdict.
            if len(word) <= len(token):
                stems.append(f"{word!r} as a stem of the corpus word {token!r}")
            else:
                stems.append(f"{word!r} as an inflection of the corpus word {token!r}")
            continue
        return None
    return stems


def _fold_map(text: str) -> tuple[str, str, list[int]]:
    """(normalised text, its folded form, folded index -> index in the first).

    `casefold` and NFKC are not length-preserving -- `ß` folds to `ss`, `ﬁ` to
    `fi` -- so an index found in the folded string does not address the same
    character in the original. `_snippet` computed its index in the folded text
    and sliced the original with it, and the snippet stored as the evidence for
    a mined term came back shifted by a character or more, or missing the term
    it was supposed to show.
    """
    base = unicodedata.normalize("NFKC", text or "")
    folded: list[str] = []
    index: list[int] = []
    for position, char in enumerate(base):
        piece = char.casefold().translate(_YO)
        folded.append(piece)
        index.extend([position] * len(piece))
    return base, "".join(folded), index


def fold(text: str) -> str:
    """Fold text the way the corpus is folded. **Public on purpose.**

    Whether the drift ban admits a term is decided by comparing folded strings,
    so anything outside this module that has to name a term -- `tg.py accept`,
    which must find the candidate the miner produced -- has to fold it exactly
    the way the corpus was folded. It was reaching for a private `_fold` with a
    fallback, and two folds that drift apart do not fail loudly: they let a
    drifting word through the one guard that exists to stop it.

    NFKC, casefold, ё -> е. Idempotent, so folding twice is folding once.
    """
    return _fold_map(text)[1].strip()


# The old private name, kept because this module says `_fold` in a dozen places
# and one spelling of the rule is the whole point of the paragraph above.
_fold = fold


def _snippet(text: str, term: str, width: int = 90) -> str:
    base, folded, index = _fold_map(text)
    pos = folded.find(term)
    if pos < 0 or pos >= len(index):
        return base[:width].replace("\n", " ").strip()
    start = max(0, index[pos] - width // 2)
    return base[start:start + width].replace("\n", " ").strip()
