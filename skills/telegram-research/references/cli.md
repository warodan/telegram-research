# CLI reference

Every subcommand and flag, every exit code, every environment variable, and the layout of a run folder.
`SKILL.md` carries the method — which question comes first, how a zero is read, what a silence is worth.
This file carries the mechanics. Read it before the first command of a run, and again whenever a result
carries a field or a code you were not expecting.

Everything below is written `tg.py …`, which means `python "$TG" …` or `python3 "$TG" …` with `$TG`
resolved by the block at the top of `SKILL.md`. The interpreter comes first and nothing comes before it.

## Every command prints JSON

The program performs acts; you make the judgements. Every subcommand prints JSON, on the way out and on the
way down — **with one hole worth knowing**: a refusal argparse makes for itself (an unknown flag, `--run` where
it is not accepted, `--max-requests 0`) exits 2 with the usage on stderr and **zero bytes on stdout**, so a
caller parsing stdout gets an empty string rather than an error object. Global flags go **before** the
subcommand: `--root <dir>` · `--run <run-dir>` · `--max-requests <n>`;
`--run` is also accepted after it on the five fetching commands (`verify`, `discover`, `search`, `history`,
`group`) and is exit 2 elsewhere. `--run <folder>` must already exist — only `newrun` creates one.
`--max-requests` counts **network acts, not commands**: a retry after a transport error is a request.

## The commands

| command | positional | flags that change the result |
| --- | --- | --- |
| `selftest` | — | `--probes` |
| `newrun` | — | `--question` (or `--brief`) `--topic` `--depth quick\|normal\|deep` `--lang` `--geo` `--since` `--until` `--seed-source`\* `--seed-query`\* `--max-rounds` `--min-new-posts` `--caller user\|agent` |
| `verify` | `usernames…` | `--write` `--found-via` (**`manual`**) `--lang` `--geo` `--min-channel-members` (100) `--min-group-members` (50) `--probe-preview` `--save-to` |
| `discover` | — | `--lyzem-query` `--lyzem-kind`\* (default **groups, channels, messages**; `all` and `bots` also accepted) `--account-query` `--from-file`\* `--text` `--found-via` (**`web`**) `--snippets-to` `--save-to` |
| `queries` | `<run> start\|record\|accept\|show` | `--query`\* `--posts` `--top` (25) `--term` `--gloss` |
| `search` | `username` | `--query`\* (**required**) `--max-pages` (5) `--save-to`. On a channel it is **silently capped at `max_pages_per_channel` (25)** and `page_ceiling` reports what was applied; on a group it counts **account calls**, 100 hits each, and has no cap |
| `history` | `username` | `--until-id` `--since-last` `--before` `--max-pages` (25) `--write` `--save-to`. Same channel cap, same `page_ceiling`. On a group a page is **one account call, up to 100 messages** |
| `group` | `username` | `--id`\* (**required**) `--save-to` |
| `note` | `run` | `--agent` `--text` `--from-file` (stdin if neither) |
| `report` | `run` | `--question` `--report-lang en\|ru` (en) `--force` |
| `accept` | `run` | — |
| `registry` | `stats\|list\|get\|compact` | `--username` (**required** for `get`) `--topic` `--type` `--limit` (50) `--force` (`compact` only) |
| `budget` | — | `--unfreeze` `--reason` |

`*` marks a repeatable flag. `--limit` and `--top` refuse `0` and negatives at exit 7: Python slices a negative
bound as "all but the last N", so a listing would drop rows while `count` still reported the true total.

## Exit codes

```
0  did what it says                    6  wrong route: a group read as a channel, or the reverse
2  usage: argparse, a path that is      7  operator error: a path, a missing file, a configuration,
   not a run folder, or NOTHING            an unreadable ledger
   WAS ASKED                            8  the run folder did not pass its own acceptance gate
3  a stop signal: Telegram said stop,   9  internal error, damaged state on disk, or selftest
   a declared ceiling fired, Ctrl-C        disagreeing with the saved probes
4  a lock is held by somebody else:    10  well-formed, and refused because it would have
   the account, or the registry guard      overwritten somebody's work (`--force` is the way through)
5  fetch failed (5xx after retries, an unexpected 4xx, or an account call that failed
   for a reason with no name of its own)
```

**A 9 from `registry` is not a bug report.** A registry holding one damaged line refuses every read at 9, and
`registry compact --force` is the single command that gets past it — the damaged file is kept as
`<registry>.bak`. That is the third thing `--force` does, and the only one of the three that is not exit 10.

**1 is never produced, and that is load-bearing.** The interpreter returns 1 for an uncaught exception, so a
deliberate 1 would be indistinguishable from a crash. Every exception reaches JSON on stdout with an
`error_type`; tracebacks go to stderr. That is also why the resolver block in `SKILL.md` refuses at **7** when
it cannot find `scripts/tg.py`: an empty `$TG` handed to the interpreter exits 1 and reads like a crash in the
skill rather than a run started from the wrong directory. **A 2 is not always a typo**: an empty or whitespace
`--query` (the transport drops it out of the URL and Telegram answers with the channel's front page, stamped as
hits for a search nobody ran), `--max-pages 0`, and a `--run` naming a directory that is not a run folder are
refusals rather than cheerful zeroes. Nothing was spent.

## `--run`, and the run folder

**`--run` is what makes a run a run.** With it, every page a result was parsed out of goes to
`<run>/notes/sources/` (the one exception is `verify --probe-preview`'s second GET, which is logged but not
saved), every request to `<run>/fetchlog.jsonl`, every post to `<run>/posts.jsonl`, and the spend accumulates
in `<run>/run.json` across all processes sharing that run. `--save-to` only **adds** a second copy. `posts.jsonl` is de-duplicated
on write by `(username, id)`, first write wins. **`--force` means two refusals, both exit 10, both about
destroying somebody's work**: `registry compact` over an existing `<registry>.bak`, and `report` over a folder
that already has a `report.md`.

```
<root>/telegram-runs/<date>-<slug>/
  brief.md · brief.json      the question and the ceilings, for a person and for every later command
  registry-delta.jsonl       sources this run added or refreshed                    (verify --write)
  queries.md · queries.json  every query by round, and what each found                    (queries)
  posts.jsonl                the posts, unredacted, one JSON object per line
  notes/<agent>.md           agent notes; a second note from the same agent is APPENDED under a
                             timestamped separator, never a replacement                     (note)
  notes/sources/             ORIGINALS: the bytes of every page read                       (--run)
  fetchlog.jsonl             one line per network act, every one kind: "fetch"; a line may carry
                             `status: 0` with an `error`, and `attempt` counts retries of one URL
  run.json                   brief, counters, stop reasons, agents, gate
  acceptance.json · report.md  the gate verdict, and the report          (accept) · (report)
```

`--topic` is a label in the brief and the registry, **not** part of the path. A second run of the same question
on the same day gets its own folder (`<slug>-2`, then `-3`), never a shared one. `notes/sources/` is what makes
a quotation checkable — it fills whenever `--run` is given, and a claim whose page was never saved cannot be
verified by anybody. Never write to `raw/`. `posts.jsonl` is deliberately **not** credential-scrubbed: it holds
fetched content, and the api_hash pattern is any 32 hex characters, which is also a commit hash or the middle
of somebody's message. Scrubbing covers exactly this list and nothing beyond it: brief, fetch log, `run.json`,
notes, `acceptance.json`, report, and the run folder's own name. `queries.md` / `queries.json` and
`registry-delta.jsonl` are not scrubbed either — like `posts.jsonl`, they hold fetched material.

**Where `<root>` comes from.** `--root` is the project run folders are created under, defaulting to the project
this skill is installed in — the first directory above the skill's own file carrying `.git` or `CLAUDE.md`.
**A skill installed globally, under `~/.claude` or `~/.agents`, does not walk up at all**: there is no project
above it, and `~/.claude/CLAUDE.md` is standard user memory rather than a project marker, so the walk would put
every run folder in the operator's home directory. A global install uses the working directory instead. Every
path printed is absolute, so the `next:` hint works from any shell. Nothing adds `telegram-runs/` to the
project's `.gitignore`; `SKILL.md` says so where it names the root.

## Depth: what `--depth` actually sets

**`--depth` is a decision, not a label** — it sets three ceilings, and `--max-rounds`, `--min-new-posts` and
`--max-requests` override any of them:

| depth | rounds | new-post floor | request ceiling |
| --- | --- | --- | --- |
| `quick` | 1 | 3 | 133 |
| `normal` | 3 | 3 | 400 |
| `deep` | 5 | 2 | 800 |

The `normal` row is the configured `budgets`, so a `TELEGRAM_RESEARCH_CONFIG` override moves all three rows at
once. A `--brief` takes its ceilings from its own `depth` for everything it does not state.

## What a post carries

Every post carries a permalink `t.me/<name>/<id>`, a date verbatim from the page, the text, the author in
whatever form the surface gives, and `ids`. Three fields exist because the text alone loses what they carry:
**`links`**, every anchor in document order as `{"text", "href"}` (text keeps an anchor's words and drops its
destination, which is the whole substance of a post whose point is a link — this also feeds
`discover --found-via link`); **`media_posters`**, the `media_urls` entries that are a still standing in for
another file; and **`found_by`**, the query that returned the post — either surface's search sets it, and a
`history` or `group` walk has no query behind it, so the `null` there is the surface being honest rather than
a parse failure. `views` decodes `views_raw` ("12.5M") and can never recover the exact figure, so keep both.

**A page gives more than the account does.** A post parsed off a web surface also carries
`reply_to_id` / `reply_to_author` / `reply_to_text` — the quoted message, lifted OUT of `text` deliberately,
and without it a group thread cannot be read back; `is_service` (a pin, a join, a title change — quoting one as
a post is a straight error); `media`, a closed set of `photo`, `video`, `document`, `voice`, `sticker`, `poll`,
`location`, `unsupported:video`; `reactions`; `forwarded_from`; `chat_peer`; and `source_file`, the saved
original it was parsed out of. **A post that arrived through the account has none of them**, because there was
no page: `source_file: null` and empty `links` on a group `search` or `history` are the route showing, not a
parse failure.

**Before believing a thin result**, read `dropped` and `silent_cuts` on `discover` (a filtered candidate is
named with its reason; a page short for a reason other than a thin index says so), `type_corrections` on
`verify`, `account_calls` / `resolves` / `peer_refreshed` on a group `search`, `mismatched_ids` on `group` (a
page that answered for another id or another peer — unlike `missing_ids`, that one is not ordinary), and
`posts_suppressed_as_duplicates` on the three reading commands. `posts_banked` is not an ordinary field: it
appears only when `search` or `history` goes down mid-walk, and says how many posts survived the fall.

## Cursors: the fields `history --write` writes, and the ones it withholds

**`--write` moves the cursor only when the walk reached an end it can prove.** A walk stopped by
`--max-pages` writes **nothing** and reports `cursor_written: false`, `cursor_withheld` (the sentence) and
`cursor_withheld_reason` (the bare code). A high-water mark written after a bounded walk makes the unread
middle unreachable for ever and then reports `reached_until_id: true` about it: the cost of not advancing is
re-reading, the cost of advancing wrongly is silent loss. Without `--write`, `cursor_written: false` is the
ordinary answer and `cursor_withheld_reason` is `null` — nothing was withheld, nothing was asked for.
**A walk stopped by the request ceiling is the one case with no cursor fields at all**: it exits 3 with
`stopped` and `posts_banked` and nothing else, so read the exit code, not the absent field. The cursor was
not written.

**`cursor_may_be_stale: true` on `history` is a warning about `--since-last`, not about this walk.** The
registry holds a line it could not read, so the stored cursor may be older than the id already reached, and
the next `--since-last` re-reads what was paid for. Fix it with `verify --write` before the next run.

The four endings a `history` walk can report are separate claims: `reached_first_post` (proven — an id at or
below 1 was actually on a page, and **the only one that means the channel is fully read**), `reached_until_id`
(caught up with stored work), `no_more_pages` (the surface published no further cursor, **not** the same
claim), `exhausted` (ended by itself rather than on a ceiling). `stop_reason` names which one ended it.

## `report` and `accept`: what they refuse

`report` writes the skeleton from what the folder holds — counters from `run.json`, mined vocabulary from
`queries.json` — and keeps apart three states it must never blur: "no word could be mined" (a query log exists
and is empty), "stage 3 did not run" (no log at all), and "the log is on disk and cannot be read" (a corrupt
one, everything else still coming from the intact files). **`report` runs once** — over a folder that already
has a `report.md` it refuses at **exit 10** and writes nothing, because a second skeleton would replace every
sentence the agent wrote and no copy exists anywhere; `--force` overwrites, so copy the prose out first.

`accept` reads nothing but the run folder — no other skill, no external script — and demands the files, at
least one non-empty note (each empty note is its own error), and a fetch log whose lines are `kind: "fetch"`.
It writes `acceptance.json` and the gate record, and exits **8** if the folder would fail. Warnings are normal
and their count is a property of the folder, not a target; `errors=0` is the thing to check.

## What lives in the state directory

`~/.telegram-research/` (or wherever `TELEGRAM_RESEARCH_STATE` points) holds `sources.jsonl` — the shared
source registry — `resolve-ledger.json`, `account.lock`, `peers.json`, `account-history.json`, `pace/`, and
the journals that sit beside them: `resolve-ledger.json.freezes.jsonl`, `account.lock.broken.jsonl`,
`sources.jsonl.bak`.

## Environment variables

| variable | what it names | default |
| --- | --- | --- |
| `TELEGRAM_RESEARCH_STATE` | the state **DIRECTORY**. Pointing it at a file is a configuration error with a sentence, **including a file that does not exist yet** | `~/.telegram-research` |
| `TELEGRAM_RESEARCH_CONFIG` | optional JSON override; `budgets` and `topics_vocabulary` are the **only** top-level keys, an unknown one is refused rather than ignored. **Account ceilings may only fall and pauses may only widen** — a value that would loosen either is clamped back and the reason lands in `budget`'s `config_notes`, not in an error | none |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION` | the credential, used only when **all three** are set; a partial set is ignored, never merged with the file | none |
| `TELEGRAM_RESEARCH_ENV` | fallback file holding those three; read only when they are not all set, and never searched for on disk | none |
| `TELEGRAM_RESEARCH_ALLOW_LIVE` | live account mode, and only for `1/true/yes/on` | off |
| `TELEGRAM_RESEARCH_TZ` | fixed offset (`+05:00`, `UTC`, `local`) for run-folder dates, registry timestamps, **the resolve ledger's day boundary and the account history's stamps** | the machine's zone |
| `TELEGRAM_RESEARCH_PROBES` | where `selftest` looks for the saved probe pages. The installed skill carries exactly the 10 it parses; the full 32-page corpus lives with the pytest suite in the project repository, and pointing this at it is the reason the variable exists | `<skill>/tests/fixtures/probes` |

**Three anchors, deliberately different.** A relative `TELEGRAM_RESEARCH_STATE` is anchored on the **home
directory** and on nothing else, because a state directory that followed the shell would mean one `cd` produces
a second, empty ledger reading "no freeze" and a second `account.lock`, so two processes hold "the" lock at
once. A relative `TELEGRAM_RESEARCH_ENV`, `TELEGRAM_RESEARCH_CONFIG` or `TELEGRAM_RESEARCH_PROBES` is anchored
on the **project** — the first directory above the skill carrying `.git` or `CLAUDE.md`, falling back to the
working directory when the skill is installed globally under `~/.claude` or `~/.agents`, or when nothing above
it carries either marker. A relative `topics_vocabulary` inside a config file is anchored on **that file**; a
path there that does not exist is a hard error and the shipped `references/topics.json` is not quietly
substituted.
**On a machine that cannot say where the home directory is (no `HOME`, no `USERPROFILE`) the skill refuses to
run**, naming `TELEGRAM_RESEARCH_STATE` as the setting that fixes it rather than inventing a directory nobody
would look in. **A configured ceiling of `0` means zero, everywhere**: absent is the only way to say "no
limit", and the account path never says it.

## Registry and discovery fields

`--found-via` defaults to `web` on `discover` and to `manual` on `verify`. Every candidate `discover` returns
carries `channels` — every discovery channel that produced that name, not only the first.

A `verify` that finds a dead name **the registry already holds** records `status: gone`, or `private` when the
name is still taken; a dead name it has never seen leaves no row at all. A write that would flip a stored
`type` is refused and records a `type_conflict` instead. `topics_vocabulary_missing: true` in the answer means
the classifier ran without its vocabulary and every source admitted in that call carries no topics —
indistinguishable afterwards from a source that has none.

## The account's own switches

Three subcommands reach the account and only three — `search <group>`, `history <group>`,
`discover --account-query`. Live mode needs two switches: `allow_live=True` in code **and**
`TELEGRAM_RESEARCH_ALLOW_LIVE` set to `1/true/yes/on`, read at every call rather than once at construction, so
turning it off stops a run already going. From the command line there is no third state: without the variable
the command refuses at exit **7**. The dry run is a Python-API mode (`AccountSession(dry_run=True)`), and it
simulates its own ceilings for `resolve`, `getHistory` and `join_group` — `contacts.search` and
`messages.search` answer "this call would go through" without consulting the ceiling at all.

## `budget`: what has been spent, and whether resolving is frozen

`tg.py budget` says what has been spent today and whether resolving is frozen. It makes no network call and
touches no credential, so it is always safe to ask, and it exits **7** with `ok: false` when the ledger cannot
be read rather than reporting a cheerful zero about a file it could not open. `--unfreeze --reason "..."` lifts
a freeze and then appends what it lifted to `<ledger>.freezes.jsonl`; that order is deliberate — a journal that
cannot be written does not un-lift a freeze the caller asked for — and its price is `recorded: false` in the
answer, meaning the freeze is gone and the decision is now unaudited. **Read that field.** The command exists
for a freeze that was never Telegram's (a wrong clock, a test) and cannot tell that from a real one. **It is
not a way to argue with a FloodWait: waiting is the only thing that ends one.**
`python "$(dirname "$TG")/account.py"` prints the same plus the account-call budget, the peer cache and
whether Telethon is installed — no network, no credential read, no state changed beyond creating the state
directory if it is not there yet, which every subcommand does.
