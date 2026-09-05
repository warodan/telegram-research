# Query craft: finding the words a community actually uses

**Nothing in this file is specific to Telegram.** The mechanism works on any
body of text where a community has its own words for things: posts with a URL
and a body are all it needs. It lives here because that is where it runs.

## The problem, in one example

Ask a Russian-language chat what people say about bribes at a driving-test centre
and search for the Russian word for *bribe*. You will find very little. What some
communities write instead is **"по рахмету"** — a borrowed word that literally
means "by thanks" — and "сдал права по рахмету" says the test was passed with one.

Ask about fines and search for *fine* — people write "сказали оплатить", they
told me to pay.

The literal word from the question finds the smaller and worse part of what was
said, and it finds it in the register of people who were being careful. The
interesting material is in the register of people who were not.

## Move 1: what a model can invent

Do it, it is cheap, and know what it cannot do.

- synonyms and near-synonyms
- colloquial and slang forms
- transliteration in both directions, and mixed alphabets in one word
- common misspellings, and deliberate misspellings used to dodge filters
- abbreviations and acronyms
- loanwords, and the local word for the borrowed thing
- the names of the specific institutions, streets, districts and companies

**"Рахмет" will never appear on this list.** Move 1 draws on what the language
contains in general; the word you need is what one community decided to call one
thing, and that is not general knowledge.

## Move 2: what the corpus knows

The agent cannot guess it. It can **see** it.

1. Take the sources found in stage 2.
2. Read a couple of hundred posts near the subject, cheaply — a `?q=` search on
   the obvious word is one request per channel.
3. Extract what words those posts use where the question used its word.

This is the whole reason sources come before posts. Move 2 has no input until a
corpus exists, and a pipeline that reads posts first has nothing to mine.

`querycraft.QueryLog.candidates()` does the mechanical half. Four rules; three of
them were bugs measured on 34 real posts of one large news channel:

1. **A term must appear in at least `min_documents` separate posts** (default
   **2**), so one person's verbal tic does not outrank a community's word.
2. **The channel's own furniture is removed before anything is counted.** A line
   standing verbatim in at least a quarter of the batch, and never fewer than 3
   posts, is a footer. «Читать нас без VPN можно здесь…» sat in 17 of the 34 posts
   and put `vpn`, `youtube`, `рассылка`, `сайт` and the channel's own handle into
   five of the top twenty, above the two words genuinely specific to the material.
   What was removed is listed as `boilerplate_lines`, never dropped in silence. A
   document that is nothing *but* one line is exempt: six people posting the same
   sentence is a community using the same words, which is the finding.
3. **Ranking is not raw frequency.** A term in every document is by construction
   either furniture or the language itself, and a document floor rewards it
   hardest. The order is `frequency × (documents + 1 − share)` — literally
   `frequency × (total − documents + 1) / total` — carried on each term as `score`
   so the order can be argued with rather than trusted.
4. **The question's own words are excluded by stem, not by exact match.** Russian
   inflection mostly replaces the ending, so «аренда» in the question did not
   exclude «аренды», and the stage whose entire purpose is to find what the
   question could NOT have said returned the question restated, tenth on the list.

Each candidate travels with the posts it came from. Everything that shortened the
answer is in `mining`: `qualified`, `returned`, `cut_by_top` (default `--top`
**25**), `below_min_documents`, `already_accepted`, `excluded_as_the_question`,
`boilerplate_lines`. **A batch smaller than the floor says so in words** — one
post can never clear a floor of two, and `[]` from it used to read as "this corpus
has no jargon".

**The judgement stays with the agent**, which is why the evidence travels with
the candidate. "Is this the local word for a bribe" is not a question frequency
can answer, and a classifier confident enough to answer it would be a classifier
nobody would check.

## Move 3 and onwards: the loop

Having found "рахмет", search on it. That yield carries the next layer:
neighbouring euphemisms, the names of the institutions, the words for the
middlemen, the price people quote. Each layer opens the next, and the subject
comes apart in strata rather than in one pass.

Every term is recorded with **where it was seen, what it means, and which round
it appeared in**. The round number is what makes the vocabulary an asset: a
second piece of research on the same subject starts with the words the first one
paid for instead of rediscovering them.

## The three stoppers

Fixed in the brief before the run starts, never negotiated mid-run. `newrun`
writes them from `--depth`, and `--max-rounds` / `--min-new-posts` /
`--max-requests` override any of them explicitly.

| stopper | the number | why that shape |
| --- | --- | --- |
| **round ceiling** | `quick` 1 · `normal` 3 · `deep` 5 | an unbounded loop is not a method, it is a hope |
| **yield floor** | `quick` 3 · `normal` 3 · `deep` 2 new posts; a round below it is the last | a floor, not "nothing new": on "nothing new" the loop lives forever on one lucky coincidence |
| **drift ban** | a new query must appear verbatim — as a whole phrase, inside one post — in text already retrieved | this is the one that keeps the run on its subject |

The `normal` row is `config.Budgets` (`max_rounds`, `min_new_posts_per_round`,
`max_requests_per_run` = 3 / 3 / 400); `quick` and `deep` are derived from it, so
a `TELEGRAM_RESEARCH_CONFIG` override moves all three rows at once.

The drift ban deserves the emphasis. Left to itself a model will generate the
next query from what the subject *reminds* it of, and three rounds later the run
is about something adjacent and reads as though it went well. `QueryLog.allows()`
checks the candidate against the actual retrieved corpus and refuses anything
absent from it — naming the refusal as drift, not as a bad idea, because it is
frequently a fine idea about a different subject.

Four details of the ban worth knowing before it surprises you:

* **It is off only while the corpus is empty**, never by round number:
  `allows()` returns True with "the question itself is the seed" when
  `corpus_tokens` is empty and checks every query otherwise. (It used to key on
  the round ledger, which meant the natural order of work — pick the queries,
  check them, *then* start the round — put every batch in the state where the ban
  was off.) In a run that starts from nothing, that is round 1 and no other round,
  which is where the shorthand "off in round 1" came from. **In a run resumed from
  a saved `queries.json` the ban is on in round 1 too**: the corpus round-trips
  through that file, so the first query of the resumed run is checked against
  every post the earlier rounds retrieved.
* **It is a PHRASE ban, not a word-by-word one.** The query's words must stand
  next to each other, in that order, inside one retrieved post. Word by word was
  the second bug, not the fix: against a corpus of "риелтор просит депозит за
  студию" and "сдал по рахмету" it admitted `рахмету студию` — a phrase occurring
  in no post at all — and handed back the sentence "found verbatim in retrieved
  text" about it. (A naked substring test, the first bug, admitted `о` and
  `да кварт` against "аренда квартиры".)
* **Short words are matched, not deleted.** Dropping words under three letters
  before sliding the window meant `arrival on visa` was admitted against a corpus
  saying "you can get a visa on arrival at the airport" and "arrival visa is cheap",
  because the survivors stood side by side in the second post. The same deletion
  made `A OR B` depend on operand order. The three-letter floor now applies to the
  query as a whole: a query with no word that long has nothing in it to check and
  is refused for that reason. `A OR B` is checked as a disjunction — every side
  separately, every side must be derivable.
* **Inflection is tolerated; a prefix is not.** A query word may match a corpus
  word it shares at least **five** letters of stem with, neither tail longer than
  **three**. `MIN_STEM` was 4 with a bare `startswith`, so any four-letter corpus
  word licensed every longer word beginning with it: «поставил стол у окна»
  cleared a search for «столица», and "the band played" cleared "bandit". Five
  keeps рахмет/рахмету and аренда/аренды, drops стол/столица and band/bandit, and
  still admits аренда/арендатор — six letters and the same root, a lead the corpus
  really did offer. `ё` and `е` fold to one letter.

## Running the loop: `tg.py queries`

Without a command for this stage the three stoppers are enforced on nobody and
`queries.md` has no writer. The bookkeeping — which is the part that drifts —
lives in one subcommand; the judgement stays with the agent.

`tg.py` below is short for `python "$TG"`. `$TG` is resolved once per shell by the
block at the top of `SKILL.md`, which is the only copy of it.

```bash
python "$TG" queries <run> start --query "..." [--query "..."]  # opens a round
python "$TG" --run <run> search <channel> --query "..."         # spend it
python "$TG" queries <run> record [--top N] [--posts <file>]    # mine, check the floor
python "$TG" queries <run> accept --term <word> --gloss "..."   # take a word
python "$TG" queries <run> show                                 # the log as it stands
```

Shortened to `tg.py <command>` everywhere else in this file.

* `start` asks `may_continue()` first: the round ceiling and the yield floor both
  refuse here, with the reason recorded in `run.json` and **exit 3**. Then every
  `--query` goes through the drift ban and the refusals are listed by name; if
  none survives, nothing is started.
* `record` reads the run's `posts.jsonl` (or `--posts <file>`), counts what is
  new by URL, and ranks candidate terms. It **excludes the question's own words**
  and every query already used — this stage exists to find what the question
  could not have said. A `--posts` path that does not exist is a refusal, not an
  empty mining run: a mistyped one was being written into the report as the fact
  that the corpus had no vocabulary.
* `accept` refuses a term that appears in no retrieved post (**exit 7**). That is
  the drift ban again, at the other end.
* Both `start` and `record` rewrite `<run>/queries.md` for the reader and
  `<run>/queries.json` for the next command. `queries.json` carries the corpus
  as well as the rounds, because a log reloaded without it would answer "the
  question itself is the seed" to everything.

A run that skips this stage is not stopped, but it is **visible**: `tg.py accept`
warns that there is no `queries.json`, so the round ceiling, the yield floor and
the drift ban bound nothing.

## What goes into the report

- **Every query, by round**, and what each one returned. `queries.md`.
- **Which queries were invented and which came out of the corpus.** A run whose
  every query was invented did not run this mechanism, and the report has to
  make that visible rather than let it pass as thoroughness.
- **Every term, with its gloss and its first sighting.** Someone has to be able
  to disagree with a gloss, and that means seeing the post it came from.
- **A round that found nothing**, kept in. A mined vocabulary that came back
  empty is a finding about the corpus, and deleting it makes the next run repeat
  the same work.

`tg.py report` writes all four from `queries.json`. It states "not one word could
be mined" **only** when a log exists and is empty, and says the stage did not run
when there is no log — the two are different facts and the report used to assert
the first one unconditionally, including in a run whose `queries.md` in the same
folder listed four mined terms with glosses.

