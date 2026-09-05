<div align="center">

# telegram-research

### Claude Code skill that reads public Telegram: channels without an account, groups with yours

[![License: MIT](https://img.shields.io/badge/License-MIT-2da44e?style=flat-square)](LICENSE)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-skill-D97757?style=flat-square)](https://code.claude.com/docs/en/skills)
[![requires python 3](https://img.shields.io/badge/requires-python%203-111111?style=flat-square)](https://www.python.org/downloads/)

**Every post comes back with a working `t.me` permalink and a date, so a claim can be checked rather than trusted.
Public channels need no account, no API key and no login at all — an account is only ever used for groups.**

[What it reads](#what-it-reads-and-how) · [Demo](#demo) · [Risks](#risks) · [Installation](#installation) · [Telegram account](#the-telegram-account-groups-only) · [Limitations](#limitations)

</div>

---

## What it reads, and how

| Surface | What you get | What it needs |
|---|---|---|
| **A public channel** | the whole history, oldest post to newest, **and** Telegram's own search across it | **nothing** — no account, no key, no login |
| **A public group** | full-text search over the entire history, with Russian morphology, and the recent messages | **your own Telegram account** — a group has no free search surface at all |
| **One known message id in a public group** | that one message | nothing — 1 GET |
| **Private chats and DMs** | never read, by design | — |

Public channels are the free half, not a reduced version of anything: `t.me/s/<name>` publishes the full history
and `?q=` searches it, the same pages anybody opens in a browser. Every post comes back as a record — permalink,
date, text, links with their anchor text, media type, reactions, views, any quoted reply, and the saved page.

Groups are the half nobody else reaches: no search engine indexes them, and the one accountless surface reads a
single id you already have, which answers about one time in a hundred — that is the whole reason the account
exists. The skill also **finds** the channels and groups that discuss a topic, and works out the words those
people use for it, usually not the words in your question.

## Demo

One real run against [`@telegram`](https://t.me/telegram), the official Telegram News channel — **no account, no
login, no credentials of any kind**. Five network requests for everything below, and `selftest` adds none.

### "What has this channel said about stickers?"

```bash
tg.py search telegram --query "sticker"
```

```json
{
  "ok": true, "username": "telegram",
  "results": [
    {
      "query": "sticker", "found": 22, "ids_seen": 22, "surface_truncated": true,
      "stop_reason": "surface_cap", "exhausted": false, "understood_nothing": false,
      "blocks_unparsed": 0, "found_nothing": false, "pages": 2, "requests": 2,
      "messages": [
        { "id": 295, "url": "https://t.me/telegram/295", "ids": [295], "date": "2024-04-29T12:28:38+00:00",
          "text": "Sticker Editor. You can turn photos on your device into custom stickers with text, drawings, emoji and more.\n\nApril Update\n1 • 2 • 3 • 4 • 5 • 6 • 7 • 8  • More",
          "links": [{ "text": "custom stickers", "href": "https://telegram.org/blog/sticker-maker" }],
          "views_raw": "3.13M", "found_by": "sticker" }

        /* 21 more posts, dated 2022-06-22 to 2026-05-14, same shape */
      ]
    }
  ],
  "requests": 2,
  "partial": true,
  "warning": "the ?q= surface capped 'sticker': these are SOME of the matches, not all of them, and the counts here must not be reported as what the channel said. Walk the history with `history` if the number matters."
}
```

Two requests, 22 dated posts, each with a permalink that opens. **The line that matters is `partial: true`**:
Telegram's search page fills one screen and stops serving, so 22 is what the surface handed over, never a count.

### "Has it ever mentioned Kubernetes?" — a zero that really is a zero

`tg.py search telegram --query "kubernetes"` — one request, and the answer says which kind of zero it is:

```json
{ "found": 0, "found_nothing": true, "exhausted": true, "understood_nothing": false, "blocks_unparsed": 0 }
```

`found_nothing: true` means the page was read and held no hits; `blocks_unparsed: 0` with `understood_nothing:
false` rules out the zero where Telegram broke the parser. Both print `found: 0`; only one means silence.

### "What is it posting right now?"

`tg.py history telegram --max-pages 1` — one request, 20 posts, each in the shape above:

```json
{ "found": 20, "requests": 1, "reached_first_post": false, "reached_until_id": false,
  "no_more_pages": false, "exhausted": false, "stop_reason": "page_ceiling", "cursor_written": false }
```

Four fields answer "did the walk reach the end?" and all four say no, so this is not a fully-read channel. And
`cursor_written: false`: the walk was cut short, and moving the "read up to here" mark would strand the middle.

### The two cheap commands around them

- `tg.py verify telegram` — 1 request: does this name exist, is it a channel or a group, how big?
- `tg.py selftest` — 0 requests: 25 assertions against saved copies of real Telegram pages.

```json
{ "username": "telegram", "exists": true, "type": "channel", "title": "Telegram News",
  "members": 9640290, "type_confirmed": true }
```

`verify` keeps the rest honest for one GET: a name that does not exist, or a group read as a channel, is caught
there. `members` is a live counter, so that figure is the reading at the moment of this run, not a fixed number.
`selftest` runs offline and says whether an empty answer means "quiet channel" or "changed front end".

**Trimmed for this page; no value above is edited**, only wrapped tighter than the printed output. Dropped: 21
of the 22 sticker posts, 19 of the 20 history posts, 8 of the 9 links on the post that is shown, and the
envelope around the three short blocks. Dropped too: `stopped_early`, `page_ceiling` and the list of queries the
surface capped; on every post `username`, `channel_title`, `views`, `chat_id` and the media, reaction and
source-file fields; on the `verify` card `taken`, `description` and `photo`; and keys that were `null` or `0`
here. Commands print more, never less.

| At a glance | |
|---|---:|
| Reads | **public channels · public groups · one known message id** |
| Public channels cost | **no account, no key, no login** |
| Groups need | **your own Telegram account — nothing else reaches them** |
| Every post carries | **a `t.me` permalink and a date** |
| Requires API keys | **not for channels · `api_id` / `api_hash` for groups** |
| Dependencies | **Python standard library; Telethon only for groups** |
| License | **MIT** |

## Risks

The free path is as ordinary as it looks. It fetches the same public pages a browser opens — `t.me/s/<name>`,
`?q=`, `?before=`, `?embed=1` — with no login, no cookies and nothing about you on Telegram's side, although the
standard source hunt also sends your search words to a third party: `lyzem.com` gets three requests, one each
for its group, channel and message index. Nothing in this repository sends a message, joins anything as a side
effect, reads a private chat, or spends money.

**Automating a personal Telegram account is a risk to that account, and that is the one place to be careful.**
The design is built around a measured incident: sixteen `contacts.resolveUsername` calls in under seven minutes
bought a 36 468-second freeze on resolving — and all sixteen returned success while the account was already
dead. So `resolveUsername` is off every ordinary path here; a group search answers with `resolves: 0` and gets
the peer from one `contacts.search` instead. Account calls are paced, counted in a durable ledger that lives
outside the skill folder, held under a single-writer lock, and bounded by daily and burst ceilings. A rate limit
from Telegram freezes the account path and is never argued with — waiting is the only thing that ends one.

The account also stays shut unless you open it: without `TELEGRAM_RESEARCH_ALLOW_LIVE` set, the three commands
that could reach it refuse before reading your credential, and nothing goes to Telegram. Joining a group is a
separate explicit operation with its own daily ceiling of three, never a side effect of a search.

## Installation

One command, and the skill is available as `/telegram-research`:

```bash
npx skills@latest add warodan/telegram-research
```

The [skills.sh](https://skills.sh/) installer asks whether to put the skill into the current project or globally
(`-g` skips the question). After installing, **restart your agent** — skills are read at startup.

### Or have your agent install it

No terminal of your own, no flags to choose — open your AI agent (Claude Code, Cursor, Codex) and paste this:

```
Install the telegram-research skill for me. Run in the terminal:
npx skills@latest add warodan/telegram-research -g -y -a <your own agent: claude-code, cursor, codex>
If npx is not found — give me a link to download Node.js and wait.
Do not install anything else.
When you are done — tell me in one line that it is ready and that the session needs a restart.
```

The agent does the rest. This is the command for the **first** install; updating uses a different one, below.

### Updating and removing

```bash
npx skills@latest update telegram-research
npx skills@latest remove telegram-research
```

Updating replaces the skill folder wholesale, which is why the working state lives in `~/.telegram-research/`
instead (see [Where it writes](#where-it-writes)); removing the skill leaves that directory behind.

## The Telegram account (groups only)

Skip this entire section unless you want to read groups. Channels never touch an account, and the skill will not
open one behind your back.

**1. Install Telethon.** It is the one borrowed dependency and the skill never installs it for you:

```bash
pip install telethon==1.44.0
```

**2. Get `api_id` and `api_hash`.** Log in at [my.telegram.org](https://my.telegram.org) → **API development
tools** and fill in the short form. The pair belongs to your Telegram account and is not a paid API key.

**3. Make a session string.** It is your login serialised into one line, so the skill never handles your phone
number or code. Telethon's own one-liner asks for the phone and the login code once, then prints the string:

```python
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
api_id, api_hash = 1234567, "the api_hash from step 2"
print(TelegramClient(StringSession(), api_id, api_hash).start().session.save())
```

Treat that string as a password: it is a logged-in session. The skill never creates one, never searches your
disk for credentials, and never writes one anywhere.

**4. Set three environment variables — all three or none**, because a partial set is ignored rather than
half-used: `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` from step 2, `TELEGRAM_SESSION` from step 3. To keep them
in a file you already have, point `TELEGRAM_RESEARCH_ENV` at it, and keep that file outside any folder that is
committed or cloud-synced. Those two routes are the only places a credential is ever read from.

**5. Turn live mode on, deliberately.** Nothing reaches the account until this is set:

```bash
TELEGRAM_RESEARCH_ALLOW_LIVE=1
```

Only `1`, `true`, `yes` or `on` count. Anything else — unset, empty, `0`, `false` — and the three account
commands refuse at exit 7 with the reason, having read no credential and sent nothing. It is read at every call,
so unsetting it stops a run already in flight.

Check the state any time with the skill's own `scripts/account.py`: no network call, no credential read, and it
prints today's spend, any freeze, and whether Telethon is installed. `tg.py budget` is the short version.

## Why

- **A zero from a scraper is ambiguous** — a quiet channel, a capped search surface and a parser Telegram broke
  all print the same `found: 0`. Separate fields tell them apart, and `selftest` answers the third offline.
- **The question rarely uses the words people use** — in some Russian-speaking chats a bribe is *rakhmet*, a
  borrowed word for "thanks". No amount of thinking produces that; one page of their own text hands it over.
- **Citations, not a summary** — permalink, date and the saved source page on every post, so a quotation can be
  checked by somebody else instead of trusted.
- **The account is the fragile part** — pacing, ceilings and a durable ledger are built in rather than left to
  whoever calls it.

## Usage

The skill picks itself up when you ask for something that fits:

```text
what do people say on Telegram about self-hosted email
read the last month of @durov and give me the posts with links
find Telegram groups that discuss homelab hardware
check this claim against Telegram sources before I repeat it
```

Or call it explicitly: `/telegram-research search @telegram for everything it has said about stickers`. Russian
phrasings trigger it too; the skill's description carries them alongside the English ones.

## How it works

1. **Scope.** `tg.py newrun` writes the question, language, geography, window and depth into a brief. The depth
   is a decision, not a label: it sets the round, new-post and request ceilings the whole run obeys.
2. **Find the sources.** Three discovery channels run together, each blind where the others are not: your
   agent's web search, the `lyzem` index, and one `contacts.search` call that costs zero resolves.
3. **Work out the words.** A couple of hundred posts near the subject are mined for what those people call the
   thing, excluding the question's own words; rounds, a drift ban and a yield floor stop the loop wandering off.
4. **Read.** Channels through their public pages, groups through the account. Every post arrives with a
   permalink, a date, the text and its links, and every page it came from is saved beside the result.
5. **Report and close.** The run folder holds the posts, the query log, the fetch log and the original pages.
   `report` builds the skeleton from the counters, and `accept` refuses a folder that would not stand up.

### Where it writes

Working state — the source registry, history cursors, the resolve ledger, the account lock and the peer cache —
lives in **`~/.telegram-research/`**, outside every project and outside the skill's own folder, so an installer
update that replaces the skill folder cannot take the record of an account freeze with it.

Run folders are created in **`telegram-runs/`** inside the project you are working in, one per run — and what
lands there is other people's data: `posts.jsonl` keeps every fetched post as it was served, including the
author id, display name and username behind each message. Add that folder to your own `.gitignore`, and delete
a run once you are done with it.

## Requirements

| Requirement | Details |
|---|---|
| An agent with skills | Claude Code, plus any agent `npx skills` installs into (Cursor, Copilot, Gemini CLI and others) |
| Python 3 | on `PATH` as `python` or `python3`. **Standard library only** — the newest stdlib call used is `str.removeprefix` (3.9); developed and tested on 3.14 |
| Node.js | only to run `npx` for the install; not needed afterwards |
| A Telegram account, plus Telethon | **groups only**, never for channels: `pip install telethon==1.44.0` and the [setup above](#the-telegram-account-groups-only). The skill never installs it and works without it |

## What's inside

```
telegram-research/                # the repository
├── skills/telegram-research/     # ← THE ONLY THING THAT GETS INSTALLED
│   ├── SKILL.md                  # the skill instructions: the three stages and the rules that matter
│   ├── references/
│   │   ├── cli.md                # every command, flag, exit code and environment variable
│   │   ├── surfaces.md           # operating manual: every Telegram surface, measured against the live site
│   │   ├── query-craft.md        # how to find the words a community actually uses
│   │   ├── account.md            # when the account may be touched, and the accounting that gates it
│   │   └── topics.json           # the topic classifier's vocabulary
│   ├── scripts/                  # tg.py and eleven modules — standard library only
│   ├── tests/fixtures/probes/    # the 10 saved pages `tg.py selftest` parses, and only those
│   └── LICENSE
├── tests/                        # the pytest suite and the full 32-page probe corpus — never installed
├── LICENSE
└── README.md
```

The installer copies `skills/telegram-research/` verbatim and nothing else; the rest of the tree stays in the
repository. That is why the suite sits outside the skill — 931 tests against a 32-page corpus, and the skill
reads none of it at runtime. The 10 pages `selftest` parses do travel with the skill, because `selftest` is a
command a user runs: an installed copy self-tests with no file from this repository.

Most of that corpus is saved pages of public Telegram channels and groups; the rest are authored stand-ins. The
captures belong to their authors and are kept here only as test material, with personal data replaced.

## Limitations

- **It is not an archive.** Registry, cursors, ledger and peer cache are working state, not a knowledge base.
- **Channel search cannot promise completeness.** Telegram's `?q=` page fills one screen and stops serving. The
  result says `partial: true` and is not a count — only a full `history` walk answers "how many times".
- **Groups need your own account, and there is no way around that.** The accountless group surface reads one
  message id you already have, and about one id in a hundred answers; no budget makes that a search.
- **No private chats, no sending, no money.** Every surface is a public page. No Stars, no Premium, no paid API
  — the paid switch is forced off after every config layer has spoken.
- **Third-party indexes are not proof of absence.** "lyzem found nothing" means its index holds nothing: 9 of 30
  names measured there were dead, and whole groups are missing from it.
- **The album branch is unguarded.** None of the probe pages carries a grouped-message wrapper, so `selftest`
  cannot notice the id-splitting for albums breaking — an album count is the one number it does not stand behind.
- **It reads; the judgement is yours.** The report skeleton is built from counters and saved pages; what a
  silence implies and what the jargon means are written by the agent, and nothing marks which sentence is which.

### When it's not a fit

| Situation | What to use instead |
|---|---|
| You want every post of a busy channel as a dataset | a dedicated exporter — this keeps working state, not an archive |
| You want to post, reply or run a bot | the Telegram Bot API — nothing here ever sends |
| The messages are in a private chat, or a group you are not a member of | nothing here reaches them; every surface is a public page |

## License

MIT — see [LICENSE](LICENSE). © 2026 Daniel Orr.

---

<div align="center">
<sub><b>Skills for Claude Code</b> · <a href="https://github.com/warodan?tab=repositories">more skills in the series</a></sub>
</div>
